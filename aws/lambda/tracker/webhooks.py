"""Verify provider messages before accepting them into the event pipeline."""

from datetime import timedelta
import hashlib
import hmac
import json

from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

from tracker.configuration import CONFIG
from tracker.database import Store
from tracker.platforms import callback_url, youtube_entries
from tracker.runtime import AWS_CLIENT, LOG, raw_body, response, timestamp, utcnow


def valid_mac(body, signature, secret, algorithm):
    """Verify a prefixed HMAC signature over the supplied bytes with constant-time comparison.

    Args:
        body (bytes): Exact byte sequence covered by the provider signature.
        signature (str | None): Received algorithm=hex-digest header value, or None when
            the header is missing.
        secret (str): Signing secret encoded as UTF-8 for the HMAC calculation.
        algorithm (str): Supported hashlib digest name, normally sha1 for YouTube or
            sha256 for Twitch.

    Returns:
        bool: Whether the received prefixed digest matches the calculated HMAC.
    """
    expected = algorithm + "=" + hmac.new(secret.encode(), body, getattr(hashlib, algorithm)).hexdigest()
    return isinstance(signature, str) and hmac.compare_digest(expected, signature)


def signing_secrets(aws, env, subscription):
    """Load active and pending SSM signing-secret versions for rotation-safe verification.

    Args:
        aws (AWS): Adapter used to retrieve versioned signing secrets from SSM.
        env (str): Logical YAML environment binding naming the platform signing-secret
            parameter.
        subscription (Mapping[str, Any]): Subscription row identifying active and
            pending SSM secret versions.

    Returns:
        list[str]: Signing-secret values for the distinct non-null active/pending
            versions.
    """
    versions = {subscription.get("secret_version"), subscription.get("pending_secret_version")}
    return [aws.setting(env, version)[0] for version in versions if version is not None]


def youtube(event, context, *, store_factory=Store.open, aws=AWS_CLIENT):
    """Validate a YouTube verification request or signed Atom notification.

    Resolve the local subscription from its query UUID. Delegate GET challenges;
    for POSTs, verify original bytes, reject unsafe or cross-channel XML, and
    publish normalized video changes before acknowledging successful delivery.
    Invalid HMACs are acknowledged and discarded under the hub protocol.

    Args:
        event (dict[str, Any]): API Gateway HTTP event containing method, subscription
            query, headers, and optional body.
        context (LambdaContext | None): Invocation context accepted for the handler
            interface; otherwise unused.
        store_factory (Callable[[], ContextManager[Store]]): Factory opening the store
            used to identify and update the subscription.
        aws (AWS): Injectable adapter for versioned signing secrets and EventBridge
            publication.

    Returns:
        dict[str, Any]: API Gateway statusCode/headers/body response, including
            challenge text when applicable.
    """
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method")
        if method not in {"GET", "POST"}:
            return response(405)
        query = event.get("queryStringParameters") or {}
        with store_factory() as store:
            sub = store.subscription(query.get("subscription_id", ""))
            if not sub or str(sub["platform"]) != "YOUTUBE":
                return response(404)
            if method == "GET":
                return youtube_challenge(store, sub, query)
            body = raw_body(event)
            headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
            secrets = signing_secrets(aws, "youtube_secret", sub)
            if not any(valid_mac(body, headers.get("x-hub-signature"), secret, "sha1") for secret in secrets):
                # Google's PubSubHubbub 0.4 protocol acknowledges invalid HMACs
                # but requires discarding them rather than processing them.
                return response(204)
            if not sub["desired"] or not sub["is_tracked"]:
                return response(204)
            details = []
            for entry in youtube_entries(body):
                if entry["platform_channel_id"] != sub["platform_channel_id"]:
                    return response(400)
                timestamp(entry["updated"])
                details.append({
                    "provider": "youtube", "kind": "video.changed", "event_key": entry["event_key"],
                    "channel_id": str(sub["channel_id"]), "video_id": entry["video_id"],
                    "occurred_at": entry["updated"],
                })
            aws.publish("youtube", details)
            return response(204)
    except (ValueError, ParseError, DefusedXmlException):
        return response(400)
    except Exception:
        LOG.exception("YouTube webhook failed")
        return response(503)


def youtube_challenge(store, sub, query):
    """Validate a pending WebSub action and return its exact hub challenge.

    Match the topic, pending action, and verification window. For subscriptions,
    record the actual granted lease and a renewal deadline capped by configured
    maximum age; only this verification resets verified_at. Also record denials
    and unsubscribe confirmations. Return an API Gateway plain-text response.

    Args:
        store (Store): Database adapter used to persist verified, denied, or
            unsubscribed state.
        sub (Mapping[str, Any]): Joined subscription/channel row including pending
            action and signing-secret version.
        query (Mapping[str, str]): Hub verification query fields: mode, topic,
            challenge, and granted lease.

    Returns:
        dict[str, Any]: Plain-text API Gateway verification response with a success or
            rejection status.
    """
    mode = query.get("hub.mode")
    if query.get("hub.topic") != sub["topic_url"] or mode not in {"subscribe", "unsubscribe", "denied"}:
        return response(404)
    if mode == "denied":
        if sub.get("pending_action"):
            store.update_subscription(sub["id"], last_error="Hub denied the subscription", status="denied")
        return response(204)
    if mode != sub.get("pending_action") or not query.get("hub.challenge"):
        return response(404)
    if not sub.get("pending_since") or utcnow() - sub["pending_since"] > timedelta(seconds=CONFIG["subscriptions"]["verification_timeout_seconds"]):
        return response(404)
    if mode == "subscribe":
        if not sub["desired"] or not sub["is_tracked"]:
            return response(404)
        lease = int(query.get("hub.lease_seconds", "0"))
        if not 0 < lease <= CONFIG["youtube"]["max_accepted_lease_seconds"]:
            return response(400)
        now = utcnow()
        store.update_subscription(
            sub["id"], status="enabled", verified_at=now,
            secret_version=sub["pending_secret_version"], pending_secret_version=None,
            lease_expires_at=now + timedelta(seconds=lease),
            renew_at=now + timedelta(seconds=min(
                lease * CONFIG["youtube"]["renewal_fraction"],
                CONFIG["youtube"]["max_refresh_interval_seconds"],
            )),
            pending_action=None, pending_since=None, retry_at=None, last_error=None, attempt_count=0,
        )
    else:
        store.update_subscription(
            sub["id"], status="unsubscribed", pending_action=None, pending_since=None,
            lease_expires_at=None, renew_at=None, retry_at=None, last_error=None,
        )
    return response(200, query["hub.challenge"])


def twitch(event, context, *, store_factory=Store.open, aws=AWS_CLIENT):
    """Authenticate and handle Twitch challenges, notifications, and revocations.

    Verify message freshness, the HMAC over ID/timestamp/original body, expected
    broadcaster/type/callback, and remote subscription identity. Challenges must
    match a recent pending registration and its pending secret. Deduplicate
    revocations transactionally and publish normal notifications to EventBridge.

    Args:
        event (dict[str, Any]): API Gateway HTTP event containing the signed Twitch
            body, delivery headers, and subscription query.
        context (LambdaContext | None): Invocation context accepted for the handler
            interface; otherwise unused.
        store_factory (Callable[[], ContextManager[Store]]): Factory opening the store
            used for subscription validation and revocation receipts.
        aws (AWS): Injectable adapter for versioned signing secrets and EventBridge
            publication.

    Returns:
        dict[str, Any]: API Gateway statusCode/headers/body response, with raw challenge
            text on verification.
    """
    try:
        if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
            return response(405)
        headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
        message_id = headers.get("twitch-eventsub-message-id", "")
        sent_at = headers.get("twitch-eventsub-message-timestamp", "")
        if not message_id or not sent_at:
            return response(400)
        age = (utcnow() - timestamp(sent_at)).total_seconds()
        if age > CONFIG["twitch"]["message_max_age_seconds"] or age < -CONFIG["twitch"]["message_future_tolerance_seconds"]:
            return response(403)
        body = raw_body(event)
        query = event.get("queryStringParameters") or {}
        with store_factory() as store:
            sub = store.subscription(query.get("subscription_id", ""))
            if not sub or str(sub["platform"]) != "TWITCH":
                return response(404)
            secrets = signing_secrets(aws, "twitch_secret", sub)
            signed = message_id.encode() + sent_at.encode() + body
            if not any(valid_mac(signed, headers.get("twitch-eventsub-message-signature"), secret, "sha256") for secret in secrets):
                return response(403)
            notification = json.loads(body)
            if not isinstance(notification, dict) or not isinstance(notification.get("subscription"), dict):
                return response(400)
            provider_sub = notification["subscription"]
            if not isinstance(provider_sub.get("condition"), dict) or not isinstance(provider_sub.get("transport"), dict):
                return response(400)
            if (
                provider_sub.get("type") != sub["event_type"]
                or provider_sub.get("version") != sub["version"]
                or provider_sub.get("condition", {}).get("broadcaster_user_id") != sub["platform_channel_id"]
                or provider_sub.get("transport", {}).get("callback") != callback_url(sub)
            ):
                return response(403)
            kind = headers.get("twitch-eventsub-message-type")
            provider_id = provider_sub.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                return response(400)
            if kind == "webhook_callback_verification":
                if not sub["desired"] or not sub["is_tracked"] or sub.get("pending_action") != "subscribe":
                    return response(403)
                if not sub.get("pending_since") or utcnow() - sub["pending_since"] > timedelta(seconds=CONFIG["subscriptions"]["verification_timeout_seconds"]):
                    return response(403)
                pending_version = sub.get("pending_secret_version")
                if pending_version is None or not valid_mac(
                    signed, headers.get("twitch-eventsub-message-signature"),
                    aws.setting("twitch_secret", pending_version)[0], "sha256",
                ):
                    return response(403)
                challenge = notification.get("challenge")
                if not isinstance(challenge, str) or not challenge:
                    return response(400)
                store.update_subscription(
                    sub["id"], provider_subscription_id=provider_id, status="verified", verified_at=utcnow(),
                    secret_version=sub["pending_secret_version"], pending_secret_version=None,
                    pending_action=None, pending_since=None, retry_at=None, last_error=None, attempt_count=0,
                )
                return response(200, challenge)
            if provider_id != sub.get("provider_subscription_id"):
                return response(403)
            if kind == "revocation":
                with store.atomic():
                    if store.receipt("twitch", message_id):
                        store.update_subscription(
                            sub["id"], status=provider_sub.get("status", "revoked"),
                            last_error="Twitch revoked the subscription", retry_at=utcnow() + timedelta(seconds=CONFIG["twitch"]["revocation_retry_seconds"]),
                        )
                return response(204)
            if kind != "notification":
                return response(400)
            if not sub["desired"] or not sub["is_tracked"]:
                return response(204)
            if not isinstance(notification.get("event"), dict) or notification["event"].get("broadcaster_user_id") != sub["platform_channel_id"]:
                return response(403)
            aws.publish("twitch", [{
                "provider": "twitch", "kind": sub["event_type"], "event_key": message_id,
                "channel_id": str(sub["channel_id"]), "occurred_at": sent_at,
            }])
            return response(204)
    except (ValueError, TypeError, KeyError):
        return response(400)
    except Exception:
        LOG.exception("Twitch webhook failed")
        return response(503)
