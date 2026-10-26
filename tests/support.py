from contextlib import nullcontext
from datetime import timedelta
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aws/lambda"))

from tracker.platforms import callback_url
from tracker.runtime import utcnow


SECRET = "a" * 64


def youtube_channel_item(channel_id="UC-channel", **overrides):
    """Build a YouTube channel API fixture with overridable profile and statistics fields.

    Args:
        channel_id (str): Canonical channel ID to place in the fake YouTube resource.
        **overrides (dict[str, Any]): Top-level fields replacing defaults in the
            channel-resource fixture.

    Returns:
        dict[str, Any]: YouTube API channel fixture containing profile and public
            statistics.
    """
    return {
        "id": channel_id,
        "snippet": {"title": "Updated channel", "description": "Channel description",
                    "thumbnails": {"high": {"url": "https://images.example/youtube.png"}}},
        "statistics": {"subscriberCount": "12300", "viewCount": "456789", "videoCount": "42", "hiddenSubscriberCount": False},
        **overrides,
    }


def twitch_user_item(user_id="1234", **overrides):
    """Build a Twitch user API fixture with overridable identity and profile fields.

    Args:
        user_id (str): Numeric Twitch user ID to place in the fake API resource.
        **overrides (dict[str, Any]): Top-level fields replacing defaults in the Twitch
            user fixture.

    Returns:
        dict[str, Any]: Public Twitch user fixture with identity, profile, and
            deprecated-view test fields.
    """
    return {"id": user_id, "display_name": "Updated Twitch", "description": "Twitch description",
            "profile_image_url": "https://images.example/twitch.png", "view_count": 0, **overrides}


def subscription(platform="YOUTUBE", **overrides):
    """Build a healthy platform subscription/channel fixture with optional field overrides.

    Args:
        platform (str): YOUTUBE or TWITCH, selecting the corresponding fixture IDs,
            URLs, and event type.
        **overrides (dict[str, Any]): Subscription or joined-channel fields that replace
            healthy fixture defaults.

    Returns:
        dict[str, Any]: Mutable joined subscription/channel fixture, including
            verification and lease timestamps.
    """
    now = utcnow()
    return {
        "id": "54d352c3-958d-4c19-9ccd-b0322fb622d3", "channel_id": "4dc32d20-4861-44b5-b063-7a00e74945ba",
        "platform": platform, "platform_channel_id": "UC-channel" if platform == "YOUTUBE" else "1234",
        "event_type": "channel.feed" if platform == "YOUTUBE" else "stream.online", "version": "1",
        "callback_url": "https://example.com/youtube/webhook" if platform == "YOUTUBE" else "https://example.com/twitch/webhook",
        "topic_url": "https://www.youtube.com/xml/feeds/videos.xml?channel_id=UC-channel",
        "desired": True, "is_tracked": True, "status": "enabled", "provider_subscription_id": "remote-1",
        "secret_version": 1, "pending_secret_version": None, "pending_action": None, "pending_since": None,
        "verified_at": now, "seeded_at": now, "lease_expires_at": now + timedelta(days=5),
        "renew_at": now + timedelta(days=4), "retry_at": None, "attempt_count": 0,
        **overrides,
    }


def store_for(sub):
    """Create a fake context-managed store whose subscription updates mutate the fixture.

    Args:
        sub (dict[str, Any]): Mutable subscription fixture returned by lookups and
            changed by fake updates.

    Returns:
        unittest.mock.MagicMock: Context-managed store mock with transaction, lookup,
            receipt, and update behavior.
    """
    store = MagicMock()
    store.__enter__.return_value = store
    store.atomic.side_effect = lambda: nullcontext()
    store.lock_job.return_value = True
    store.subscription.return_value = sub
    store.subscriptions.return_value = [sub]
    store.channels.return_value = []
    store.receipt.return_value = True
    store.update_subscription.side_effect = lambda _id, **fields: sub.update(fields)
    return store


def aws_mock():
    """Return an AWS mock supplying a deterministic test-only secret and SSM version.

    Returns:
        unittest.mock.MagicMock: AWS adapter mock returning the deterministic test
            signing secret and version.
    """
    aws = MagicMock()
    aws.setting.return_value = (SECRET, 1)
    return aws


def api_event(body="", *, method="POST", sub=None, headers=None, encoded=False):
    """Build an API Gateway HTTP event, optionally encoding its body as base64.

    Args:
        body (str): Original request content to store directly or encode as base64.
        method (str): HTTP method written into the API Gateway request context.
        sub (Mapping[str, Any] | None): Optional subscription fixture whose local id
            populates the query string.
        headers (dict[str, str] | None): Optional HTTP request headers; None produces an
            empty mapping.
        encoded (bool): Whether to base64-encode the body and set isBase64Encoded.

    Returns:
        dict[str, Any]: API Gateway HTTP event fixture suitable for webhook handler
            invocation.
    """
    return {
        "requestContext": {"http": {"method": method}},
        "body": base64.b64encode(body.encode()).decode() if encoded else body,
        "isBase64Encoded": encoded,
        "headers": headers or {},
        "queryStringParameters": {"subscription_id": sub["id"]} if sub else {},
    }


def feed(channel="UC-channel", video="video-1", title="A video"):
    """Build a minimal YouTube Atom feed for a chosen channel, video, and title.

    Args:
        channel (str): YouTube channel ID embedded in the Atom entry.
        video (str): YouTube video ID embedded in the Atom entry.
        title (str): Video title text included in the entry and event fingerprint.

    Returns:
        str: Minimal Atom XML containing one timestamped YouTube entry.
    """
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
    <entry><yt:videoId>{video}</yt:videoId><yt:channelId>{channel}</yt:channelId>
    <title>{title}</title><updated>2026-10-26T12:00:00Z</updated></entry></feed>'''


def youtube_event(sub, body=None, encoded=False):
    """Build a YouTube request signed over the original feed bytes with the test secret.

    Args:
        sub (Mapping[str, Any]): Subscription fixture identifying the local callback
            query UUID.
        body (str | None): Atom XML to sign, or None to use the default feed fixture.
        encoded (bool): Whether the signed original body is transported in base64 form.

    Returns:
        dict[str, Any]: YouTube API Gateway event carrying the test HMAC-SHA1 signature.
    """
    body = feed() if body is None else body
    signature = "sha1=" + hmac.new(SECRET.encode(), body.encode(), hashlib.sha1).hexdigest()
    return api_event(body, sub=sub, headers={"X-Hub-Signature": signature}, encoded=encoded)


def twitch_event(sub, *, kind="notification", message_id="message-1", sent_at=None, encoded=False, **overrides):
    """Build a signed Twitch event with configurable type, freshness, ID, and payload fields.

    Args:
        sub (Mapping[str, Any]): Joined subscription/channel fixture defining the remote
            registration and broadcaster.
        kind (str): Twitch message type, such as notification,
            webhook_callback_verification, or revocation.
        message_id (str): Delivery ID included in the signature and used for
            deduplication.
        sent_at (str | None): Signed ISO timestamp, or None to use the current UTC time.
        encoded (bool): Whether the original signed JSON body is transported in base64
            form.
        **overrides (dict[str, Any]): Top-level JSON payload replacements, such as
            event, subscription, or challenge.

    Returns:
        dict[str, Any]: Twitch API Gateway event carrying delivery headers and a valid
            test HMAC-SHA256.
    """
    sent_at = sent_at or utcnow().isoformat()
    payload = {
        "subscription": {
            "id": sub["provider_subscription_id"] or "remote-1", "type": sub["event_type"], "version": "1",
            "status": "enabled", "condition": {"broadcaster_user_id": sub["platform_channel_id"]},
            "transport": {"method": "webhook", "callback": callback_url(sub)},
        },
        "event": {"broadcaster_user_id": sub["platform_channel_id"]},
        **overrides,
    }
    body = json.dumps(payload)
    signed = (message_id + sent_at + body).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return api_event(body, sub=sub, encoded=encoded, headers={
        "Twitch-Eventsub-Message-Id": message_id,
        "Twitch-Eventsub-Message-Timestamp": sent_at,
        "Twitch-Eventsub-Message-Type": kind,
        "Twitch-Eventsub-Message-Signature": signature,
    })
