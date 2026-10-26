"""Idempotent WebSub renewal and EventSub reconciliation jobs."""

from datetime import timedelta
import time
from urllib.parse import urlsplit

from tracker.database import Store
from tracker.configuration import CONFIG, environment
from tracker.platforms import PLATFORMS, callback_url, youtube_entries, youtube_topic
from tracker.runtime import AWS_CLIENT, HTTP_CLIENT, ApiError, LOG, has_time, timestamp, utcnow


TERMINAL_TWITCH = set(CONFIG["twitch"]["terminal_subscription_statuses"])


def configured_callback(name):
    value = environment(name)
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.port not in {None, 443} or parts.query or parts.fragment:
        raise ValueError("Callback must be a public HTTPS URL on port 443 without a query or fragment")
    return value


def retry_later(store, sub, error):
    attempt = sub["attempt_count"] + 1
    settings = CONFIG["subscriptions"]
    delay = min(settings["retry_max_seconds"], settings["retry_base_seconds"] * 2 ** min(attempt, settings["retry_exponent_cap"]))
    if isinstance(error, ApiError):
        delay = max(delay, error.retry_after)
    store.update_subscription(
        sub["id"], retry_at=utcnow() + timedelta(seconds=delay),
        attempt_count=attempt, last_error=type(error).__name__ + (f": HTTP {error.status}" if isinstance(error, ApiError) else ""),
    )
    LOG.error("Subscription attempt failed: id=%s error=%s", sub["id"], type(error).__name__)


def ready(sub, now):
    return not sub.get("retry_at") or sub["retry_at"] <= now


def websub(event, context, *, store_factory=Store.open, aws=AWS_CLIENT, http=HTTP_CLIENT):
    started = time.monotonic()
    base = configured_callback("youtube_callback")
    secret, version = aws.setting("youtube_secret")
    if not 1 <= len(secret.encode()) < 200:
        raise ValueError("YouTube signing secret must contain 1 to 199 UTF-8 bytes")
    counts = {"attempted": 0, "failed": 0, "deferred": 0}
    with store_factory() as store:
        if not store.lock_job("subscribe-to-websub"):
            return {"skipped": "already running"}
        for channel in store.channels("YOUTUBE"):
            store.ensure_subscription(channel, "channel.feed", base, youtube_topic(channel["platform_channel_id"]))
        rows = store.subscriptions("YOUTUBE")
        for sub in rows:
            now = utcnow()
            if not ready(sub, now):
                continue
            if sub["pending_since"] and now - sub["pending_since"] < timedelta(seconds=CONFIG["subscriptions"]["pending_retry_seconds"]):
                continue
            if sub["desired"]:
                due = sub["renew_at"] is None or sub["renew_at"] <= now
                due = due or sub["secret_version"] != version or sub["callback_url"] != base
                due = due or sub["seeded_at"] is None
                if not due:
                    continue
                mode = "subscribe"
                # First remove the old callback registration before switching URLs.
                if sub["callback_url"] != base and sub["lease_expires_at"] and sub["lease_expires_at"] > now:
                    mode = "unsubscribe"
            else:
                if sub["status"] == "unsubscribed" or (
                    sub["lease_expires_at"] and sub["lease_expires_at"] <= now
                ):
                    continue
                mode = "unsubscribe"
            if counts["attempted"] >= CONFIG["subscriptions"]["batch_size"] or not has_time(context, started):
                counts["deferred"] += 1
                continue
            counts["attempted"] += 1
            try:
                if mode == "subscribe" and sub["callback_url"] != base:
                    store.update_subscription(sub["id"], callback_url=base)
                    sub["callback_url"] = base
                store.update_subscription(
                    sub["id"], pending_action=mode, pending_since=now,
                    pending_secret_version=version, retry_at=now + timedelta(seconds=CONFIG["subscriptions"]["pending_retry_seconds"]),
                    status="enabled" if sub["lease_expires_at"] and sub["lease_expires_at"] > now else "pending",
                )
                http.request("POST", CONFIG["youtube"]["hub_url"], form={
                    "hub.mode": mode, "hub.topic": sub["topic_url"],
                    "hub.callback": callback_url(sub), "hub.secret": secret,
                    "hub.lease_seconds": CONFIG["youtube"]["requested_lease_seconds"],
                })
                if mode == "subscribe" and sub["seeded_at"] is None:
                    # WebSub is not a historical backfill API. Seed recent public
                    # videos once so already-scheduled/live streams can be found.
                    entries = youtube_entries(http.request("GET", sub["topic_url"]))
                    aws.publish("youtube", [{
                        "provider": "youtube", "kind": "video.changed", "event_key": item["event_key"],
                        "channel_id": str(sub["channel_id"]), "video_id": item["video_id"],
                        "occurred_at": item["updated"],
                    } for item in entries if item["platform_channel_id"] == sub["platform_channel_id"]])
                    store.update_subscription(sub["id"], seeded_at=utcnow())
            except Exception as error:
                counts["failed"] += 1
                retry_later(store, sub, error)
    LOG.info("WebSub reconciliation: %s", counts)
    if counts["failed"]:
        raise RuntimeError(f"WebSub reconciliation had {counts['failed']} failed attempts; retries are persisted")
    return counts


def owned_remote(remote, sub):
    return (
        remote.get("type") == sub["event_type"] and remote.get("version") == sub["version"]
        and remote.get("condition", {}).get("broadcaster_user_id") == sub["platform_channel_id"]
        and remote.get("transport", {}).get("method") == "webhook"
        and remote["transport"].get("callback") == callback_url(sub)
    )


def delete_remote(platforms, remote):
    try:
        platforms.twitch("DELETE", CONFIG["twitch"]["subscriptions_path"], params={"id": remote["id"]})
    except ApiError as error:
        if error.status != 404:
            raise


def eventsub(event, context, *, store_factory=Store.open, aws=AWS_CLIENT, platforms=PLATFORMS):
    started = time.monotonic()
    base = configured_callback("twitch_callback")
    secret, version = aws.setting("twitch_secret")
    if not secret.isascii() or not 10 <= len(secret) <= 100:
        raise ValueError("Twitch signing secret must contain 10 to 100 ASCII characters")
    counts = {"created": 0, "enabled": 0, "removed": 0, "failed": 0, "deferred": 0}
    with store_factory() as store:
        if not store.lock_job("subscribe-to-eventsub"):
            return {"skipped": "already running"}
        for channel in store.channels("TWITCH"):
            for event_type in ("stream.online", "stream.offline"):
                store.ensure_subscription(channel, event_type, base)
        remote = platforms.twitch_subscriptions()
        actions = 0
        for sub in store.subscriptions("TWITCH"):
            if not ready(sub, utcnow()):
                continue
            if not has_time(context, started) or actions >= CONFIG["subscriptions"]["batch_size"]:
                counts["deferred"] += 1
                continue
            try:
                matches = [item for item in remote if owned_remote(item, sub)]
                matching = next((item for item in matches if item["status"] == "enabled"), None)
                replace = sub["callback_url"] != base or sub["secret_version"] not in {None, version}
                if sub["desired"] and matching and not replace:
                    store.update_subscription(
                        sub["id"], status="enabled", provider_subscription_id=matching["id"],
                        secret_version=sub["secret_version"] or sub["pending_secret_version"] or version,
                        pending_secret_version=None, pending_action=None, pending_since=None,
                        retry_at=None, last_error=None, attempt_count=0,
                    )
                    counts["enabled"] += 1
                    continue
                if sub["desired"] and not replace and any(
                    item["status"] == "webhook_callback_verification_pending"
                    and utcnow() - timestamp(item["created_at"]) < timedelta(seconds=CONFIG["subscriptions"]["pending_retry_seconds"])
                    for item in matches
                ):
                    continue
                if sub["desired"] and (sub["status"] in TERMINAL_TWITCH or any(item["status"] in TERMINAL_TWITCH for item in matches)):
                    continue
                for item in matches:
                    delete_remote(platforms, item)
                    counts["removed"] += 1
                if not sub["desired"]:
                    store.update_subscription(
                        sub["id"], status="unsubscribed", provider_subscription_id=None,
                        pending_action=None, pending_since=None, retry_at=None,
                    )
                    continue
                now = utcnow()
                store.update_subscription(
                    sub["id"], callback_url=base, pending_action="subscribe", pending_since=now,
                    pending_secret_version=version, status="pending", retry_at=now + timedelta(seconds=CONFIG["subscriptions"]["pending_retry_seconds"]),
                    provider_subscription_id=None,
                )
                sub["callback_url"] = base
                actions += 1
                try:
                    result = platforms.twitch("POST", CONFIG["twitch"]["subscriptions_path"], payload={
                        "type": sub["event_type"], "version": sub["version"],
                        "condition": {"broadcaster_user_id": sub["platform_channel_id"]},
                        "transport": {"method": "webhook", "callback": callback_url(sub), "secret": secret},
                    })
                except ApiError as error:
                    if error.status == 409:
                        remote = platforms.twitch_subscriptions()
                        continue
                    raise
                created = result["data"][0]
                store.record_twitch_created(sub["id"], created["id"], created["status"])
                counts["created"] += 1
            except Exception as error:
                counts["failed"] += 1
                retry_later(store, sub, error)
    LOG.info("EventSub reconciliation: %s", counts)
    if counts["failed"]:
        raise RuntimeError(f"EventSub reconciliation had {counts['failed']} failed attempts; retries are persisted")
    return counts
