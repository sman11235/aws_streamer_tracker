"""Idempotent WebSub renewal and EventSub reconciliation jobs."""

from datetime import timedelta
import time
from urllib.parse import urlsplit

from tracker.database import Store
from tracker.configuration import CONFIG, environment
from tracker.platforms import PLATFORMS, callback_url, youtube_entries, youtube_topic
from tracker.runtime import AWS_CLIENT, HTTP_CLIENT, ApiError, LOG, has_time, logged_job, timestamp, utcnow


TERMINAL_TWITCH = set(CONFIG["twitch"]["terminal_subscription_statuses"])


def configured_callback(name):
    """Resolve a callback environment binding and validate its public HTTPS shape.

    Require a hostname and port 443, with no query or fragment in the base URL.
    Raise ValueError for invalid callback configuration.

    Args:
        name (str): Logical runtime-YAML environment binding identifying the platform
            callback base URL.

    Returns:
        str: Validated HTTPS callback base URL on port 443, without query or fragment.
    """
    value = environment(name)
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.port not in {None, 443} or parts.query or parts.fragment:
        raise ValueError("Callback must be a public HTTPS URL on port 443 without a query or fragment")
    return value


def retry_later(store, sub, error):
    """Persist exponential retry backoff and a safe error summary for a subscription.

    Increment the attempt counter and honor longer API retry delays. Log only
    subscription/channel IDs, error class, HTTP status, and retry timing;
    do not sleep inside the invocation or log exception text.

    Args:
        store (Store): Database adapter used to persist retry state.
        sub (Mapping[str, Any]): Subscription row containing its local id and previous
            attempt_count.
        error (Exception): Failure that triggered backoff; ApiError may supply a longer
            retry delay.

    Returns:
        None: The retry timestamp, attempt count, and safe error summary are saved and
            logged.
    """
    attempt = sub["attempt_count"] + 1
    settings = CONFIG["subscriptions"]
    delay = min(settings["retry_max_seconds"], settings["retry_base_seconds"] * 2 ** min(attempt, settings["retry_exponent_cap"]))
    if isinstance(error, ApiError):
        delay = max(delay, error.retry_after)
    store.update_subscription(
        sub["id"], retry_at=utcnow() + timedelta(seconds=delay),
        attempt_count=attempt, last_error=type(error).__name__ + (f": HTTP {error.status}" if isinstance(error, ApiError) else ""),
    )
    LOG.error("Subscription attempt failed: id=%s channel_id=%s event_type=%s error=%s http_status=%s attempt=%d retry_in_seconds=%d",
              sub["id"], sub["channel_id"], sub["event_type"], type(error).__name__,
              error.status if isinstance(error, ApiError) else None, attempt, delay)


def ready(sub, now):
    """Return whether the subscription has no retry delay or its delay has elapsed.

    Args:
        sub (Mapping[str, Any]): Subscription row containing an optional timezone-aware
            retry_at timestamp.
        now (datetime.datetime): Timezone-aware current time used to compare the retry
            deadline.

    Returns:
        bool: True when no retry is scheduled or the retry deadline has passed.
    """
    return not sub.get("retry_at") or sub["retry_at"] <= now


@logged_job("subscribe-to-websub")
def websub(event, context, *, store_factory=Store.open, aws=AWS_CLIENT, http=HTTP_CLIENT):
    """Reconcile tracked YouTube channels with their WebSub registrations.

    Create missing local records, renew due leases, enforce the configured
    maximum age since verification, and unsubscribe undesired registrations.
    Persist pending state before hub requests so immediate callbacks can verify
    it. Seed recent public videos once after registration and preserve progress
    when another channel fails. A hub's acceptance is not verification.

    Args:
        event (dict[str, Any]): Scheduler payload; no event fields are required.
        context (LambdaContext | None): Invocation context used to stop work before
            timeout, or None for local execution.
        store_factory (Callable[[], ContextManager[Store]]): Factory opening a
            context-managed subscription store.
        aws (AWS): Injectable adapter resolving the signing secret and publishing seeded
            video events.
        http (HTTP): Injectable HTTP client for hub registration requests and
            recent-feed downloads.

    Returns:
        dict[str, int | str]: Attempted/failed/deferred counters, or a skipped reason
            for an overlapping manager.

    Raises:
        RuntimeError: An attempt failed after its retry state was persisted.
    """
    started = time.monotonic()
    base = configured_callback("youtube_callback")
    secret, version = aws.setting("youtube_secret")
    if not 1 <= len(secret.encode()) < 200:
        raise ValueError("YouTube signing secret must contain 1 to 199 UTF-8 bytes")
    counts = {"attempted": 0, "failed": 0, "deferred": 0}
    waiting = {"healthy": 0, "retry": 0, "verification": 0, "inactive": 0}
    actions = {"subscribe": 0, "unsubscribe": 0, "seeded_events": 0}
    with store_factory() as store:
        if not store.lock_job("subscribe-to-websub"):
            return {"skipped": "already running"}
        channels = store.channels("YOUTUBE")
        for channel in channels:
            store.ensure_subscription(channel, "channel.feed", base, youtube_topic(channel["platform_channel_id"]))
        rows = store.subscriptions("YOUTUBE")
        LOG.info("WebSub work selected: channels=%d subscriptions=%d attempt_limit=%d",
                 len(channels), len(rows), CONFIG["subscriptions"]["batch_size"])
        for sub in rows:
            now = utcnow()
            if not ready(sub, now):
                waiting["retry"] += 1
                continue
            if sub["pending_since"] and now - sub["pending_since"] < timedelta(seconds=CONFIG["subscriptions"]["pending_retry_seconds"]):
                waiting["verification"] += 1
                continue
            if sub["desired"]:
                due = sub["renew_at"] is None or sub["renew_at"] <= now
                # Enforce the age cap for existing rows too, even if their
                # stored renewal date was calculated from a much longer lease.
                verified_at = sub.get("verified_at")
                due = due or verified_at is None or now >= verified_at + timedelta(seconds=CONFIG["youtube"]["max_refresh_interval_seconds"])
                due = due or sub["secret_version"] != version or sub["callback_url"] != base
                due = due or sub["seeded_at"] is None
                if not due:
                    waiting["healthy"] += 1
                    continue
                mode = "subscribe"
                # First remove the old callback registration before switching URLs.
                if sub["callback_url"] != base and sub["lease_expires_at"] and sub["lease_expires_at"] > now:
                    mode = "unsubscribe"
            else:
                if sub["status"] == "unsubscribed" or (
                    sub["lease_expires_at"] and sub["lease_expires_at"] <= now
                ):
                    waiting["inactive"] += 1
                    continue
                mode = "unsubscribe"
            if counts["attempted"] >= CONFIG["subscriptions"]["batch_size"] or not has_time(context, started):
                counts["deferred"] += 1
                continue
            counts["attempted"] += 1
            LOG.debug("WebSub attempt: subscription_id=%s channel_id=%s action=%s", sub["id"], sub["channel_id"], mode)
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
                actions[mode] += 1
                if mode == "subscribe" and sub["seeded_at"] is None:
                    # WebSub is not a historical backfill API. Seed recent public
                    # videos once so already-scheduled/live streams can be found.
                    entries = youtube_entries(http.request("GET", sub["topic_url"]))
                    details = [{
                        "provider": "youtube", "kind": "video.changed", "event_key": item["event_key"],
                        "channel_id": str(sub["channel_id"]), "video_id": item["video_id"],
                        "occurred_at": item["updated"],
                    } for item in entries if item["platform_channel_id"] == sub["platform_channel_id"]]
                    aws.publish("youtube", details)
                    actions["seeded_events"] += len(details)
                    store.update_subscription(sub["id"], seeded_at=utcnow())
            except Exception as error:
                counts["failed"] += 1
                retry_later(store, sub, error)
    LOG.info("WebSub reconciliation: counts=%s accepted_actions=%s waiting=%s deferred_reason=attempt_limit_or_invocation_budget",
             counts, actions, waiting)
    if counts["failed"]:
        raise RuntimeError(f"WebSub reconciliation had {counts['failed']} failed attempts; retries are persisted")
    return counts


def owned_remote(remote, sub):
    """Return whether a Twitch registration matches this channel, type, and callback.

    Args:
        remote (Mapping[str, Any]): Twitch API subscription object whose ownership
            should be checked.
        sub (Mapping[str, Any]): Local subscription/channel fields defining expected
            type, version, broadcaster, and callback.

    Returns:
        bool: Whether every ownership-defining remote field matches this local
            registration.
    """
    return (
        remote.get("type") == sub["event_type"] and remote.get("version") == sub["version"]
        and remote.get("condition", {}).get("broadcaster_user_id") == sub["platform_channel_id"]
        and remote.get("transport", {}).get("method") == "webhook"
        and remote["transport"].get("callback") == callback_url(sub)
    )


def delete_remote(platforms, remote):
    """Delete a Twitch registration, treating HTTP 404 as already removed.

    Args:
        platforms (Platforms): Twitch API adapter used to delete the remote
            subscription.
        remote (Mapping[str, Any]): Remote subscription object containing its Twitch id.

    Returns:
        None: The subscription was deleted or already absent; other API errors
            propagate.
    """
    try:
        platforms.twitch("DELETE", CONFIG["twitch"]["subscriptions_path"], params={"id": remote["id"]})
    except ApiError as error:
        if error.status != 404:
            raise


@logged_job("subscribe-to-eventsub")
def eventsub(event, context, *, store_factory=Store.open, aws=AWS_CLIENT, platforms=PLATFORMS):
    """Reconcile online/offline Twitch registrations for tracked broadcasters.

    List remote subscriptions, adopt healthy owned matches, replace changed
    callbacks or signing keys, and remove undesired owned registrations. Keep
    verification callbacks authoritative if they race the create response.
    Respect retry delays, per-invocation limits, and terminal revocation states.

    Args:
        event (dict[str, Any]): Scheduler payload; no event fields are required.
        context (LambdaContext | None): Invocation context used to reserve completion
            time, or None for local execution.
        store_factory (Callable[[], ContextManager[Store]]): Factory opening a
            context-managed subscription store.
        aws (AWS): Injectable adapter resolving the Twitch signing secret and its SSM
            version.
        platforms (Platforms): Injectable Twitch client used to list, create, and delete
            EventSub registrations.

    Returns:
        dict[str, int | str]: Created/enabled/removed/failed/deferred counters, or an
            overlapping-job skip reason.

    Raises:
        RuntimeError: Reconciliation failures remain after saving retry state.
    """
    started = time.monotonic()
    base = configured_callback("twitch_callback")
    secret, version = aws.setting("twitch_secret")
    if not secret.isascii() or not 10 <= len(secret) <= 100:
        raise ValueError("Twitch signing secret must contain 10 to 100 ASCII characters")
    counts = {"created": 0, "enabled": 0, "removed": 0, "failed": 0, "deferred": 0}
    waiting = {"retry": 0, "verification": 0, "terminal": 0, "create_conflicts": 0}
    with store_factory() as store:
        if not store.lock_job("subscribe-to-eventsub"):
            return {"skipped": "already running"}
        channels = store.channels("TWITCH")
        for channel in channels:
            for event_type in ("stream.online", "stream.offline"):
                store.ensure_subscription(channel, event_type, base)
        remote = platforms.twitch_subscriptions()
        rows = store.subscriptions("TWITCH")
        LOG.info("EventSub work selected: channels=%d subscriptions=%d remote_total=%d remote_enabled=%d attempt_limit=%d",
                 len(channels), len(rows), len(remote), sum(item["status"] == "enabled" for item in remote),
                 CONFIG["subscriptions"]["batch_size"])
        actions = 0
        for sub in rows:
            if not ready(sub, utcnow()):
                waiting["retry"] += 1
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
                    waiting["verification"] += 1
                    continue
                if sub["desired"] and (sub["status"] in TERMINAL_TWITCH or any(item["status"] in TERMINAL_TWITCH for item in matches)):
                    waiting["terminal"] += 1
                    LOG.debug("EventSub requires intervention: subscription_id=%s channel_id=%s", sub["id"], sub["channel_id"])
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
                LOG.debug("EventSub create: subscription_id=%s channel_id=%s event_type=%s", sub["id"], sub["channel_id"], sub["event_type"])
                try:
                    result = platforms.twitch("POST", CONFIG["twitch"]["subscriptions_path"], payload={
                        "type": sub["event_type"], "version": sub["version"],
                        "condition": {"broadcaster_user_id": sub["platform_channel_id"]},
                        "transport": {"method": "webhook", "callback": callback_url(sub), "secret": secret},
                    })
                except ApiError as error:
                    if error.status == 409:
                        waiting["create_conflicts"] += 1
                        remote = platforms.twitch_subscriptions()
                        continue
                    raise
                created = result["data"][0]
                store.record_twitch_created(sub["id"], created["id"], created["status"])
                counts["created"] += 1
            except Exception as error:
                counts["failed"] += 1
                retry_later(store, sub, error)
    if waiting["terminal"]:
        LOG.warning("EventSub subscriptions require intervention: count=%d", waiting["terminal"])
    LOG.info("EventSub reconciliation: counts=%s waiting=%s deferred_reason=attempt_limit_or_invocation_budget",
             counts, waiting)
    if counts["failed"]:
        raise RuntimeError(f"EventSub reconciliation had {counts['failed']} failed attempts; retries are persisted")
    return counts
