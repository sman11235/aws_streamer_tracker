"""Scheduled channel profile/statistics refresh with atomic history snapshots."""

from datetime import timezone
from itertools import zip_longest
import time
from uuid import uuid4

from tracker.configuration import CONFIG
from tracker.database import Store
from tracker.platforms import PLATFORMS
from tracker.runtime import LOG, has_time, timestamp, utcnow
from tracker.streams import chunks


def optional_count(value):
    """Normalize a nonnegative integer or digit string, preserving None as unknown.

    Reject booleans, negative values, fractional values, and malformed strings
    with ValueError rather than storing fabricated or truncated counts.

    Args:
        value (int | str | None): Reported count as a nonnegative integer or digit
            string, or None when unavailable.

    Returns:
        int | None: Normalized count, preserving None as unknown and accepting a genuine
            zero.
    """
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("Channel counts must be nonnegative integers")


def youtube_channel(item):
    """Normalize a YouTube channel profile and its available public statistics.

    Choose a thumbnail using the configured preference order. Preserve hidden
    or omitted counts as None and retain the API's subscriber-count precision.
    Raise ValueError when the name or any supplied count is invalid.

    Args:
        item (dict[str, Any]): YouTube channel resource with snippet and optional
            statistics fields.

    Returns:
        dict[str, str | int | None]: Normalized profile and public counts;
            hidden/omitted metrics remain None.
    """
    snippet = item["snippet"]
    name = snippet["title"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("YouTube channel name is missing")
    statistics = item.get("statistics", {})
    thumbnails = snippet.get("thumbnails", {})
    picture = next((thumbnails[size]["url"] for size in CONFIG["youtube"]["thumbnail_priority"] if size in thumbnails), None)
    return {
        "channel_name": name, "description": snippet.get("description"), "profile_picture_url": picture,
        # Public YouTube subscriber counts are rounded by the API.
        "subscriber_count": None if statistics.get("hiddenSubscriberCount") else optional_count(statistics.get("subscriberCount")),
        "video_count": optional_count(statistics.get("videoCount")),
        "total_views": optional_count(statistics.get("viewCount")),
    }


def twitch_channel(item):
    """Normalize a Twitch profile, storing its follower count as subscriber_count.

    Use the supplied follower_count for the shared audience-count field. Preserve
    missing followers as None and reported zero as zero. Keep video totals and
    deprecated channel views as None. Raise ValueError for a missing display name
    or an invalid follower count.

    Args:
        item (dict[str, Any]): Twitch Get Users resource containing display_name and
            optional profile fields, optionally enriched with follower_count as a
            nonnegative integer or digit string. Get Users does not supply it.

    Returns:
        dict[str, str | int | None]: Normalized public profile with subscriber_count
            representing followers, and unavailable count fields set to None.
    """
    name = item["display_name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Twitch channel name is missing")
    return {
        "channel_name": name, "description": item.get("description"),
        "profile_picture_url": item.get("profile_image_url") or None,
        # The shared audience-count field stores followers for Twitch.
        "subscriber_count": optional_count(item.get("follower_count")),
        "video_count": None, "total_views": None,
    }


def update(event, context, *, store_factory=Store.open, platforms=PLATFORMS):
    """Discover linked streamer channels, refresh profiles, and record history.

    Discover missing rows from streamer_info, alternate provider batches, and
    save each valid response with its history snapshot atomically. Skip already
    completed scheduled work, preserve missing-channel data, and retain successful
    snapshots when another channel fails.

    Args:
        event (dict[str, Any]): Scheduler payload with optional scheduled_at ISO
            timestamp; omission creates a new manual run key.
        context (LambdaContext | None): Invocation context used to defer work near
            timeout, or None for the local time budget.
        store_factory (Callable[[], ContextManager[Store]]): Factory opening a store for
            discovery, refresh selection, and atomic history updates.
        platforms (Platforms): Injectable adapter supplying batched YouTube channel and
            Twitch user lookups, plus a follower-total lookup per Twitch broadcaster.

    Returns:
        dict[str, int | str]: Discovery/refresh outcome counters, or an overlapping-job
            skipped reason.

    Raises:
        RuntimeError: Some channel updates failed and need retrying.
    """
    started = time.monotonic()
    scheduled_at = event.get("scheduled_at")
    collection_key = (
        "scheduled:" + timestamp(scheduled_at).astimezone(timezone.utc).isoformat()
        if scheduled_at else "manual:" + str(uuid4())
    )
    counts = {"discovered": 0, "updated": 0, "duplicate": 0, "stale": 0, "untracked": 0,
              "missing": 0, "failed": 0, "deferred": 0}
    with store_factory() as store:
        if not store.lock_job("update-channels"):
            return {"skipped": "already running"}
        # IDs are authoritative even when the legacy has_* flags have not yet
        # been updated. Conflict handling preserves explicit tracking opt-outs.
        counts["discovered"] = store.discover_channels()
        settings = CONFIG["channel_updates"]
        youtube = chunks(store.channels_for_refresh("YOUTUBE", collection_key), settings["youtube_batch_size"])
        twitch = chunks(store.channels_for_refresh("TWITCH", collection_key), settings["twitch_batch_size"])
        batches = [(provider, batch) for pair in zip_longest(youtube, twitch)
                   for provider, batch in zip(("youtube", "twitch"), pair) if batch]
        for provider, batch in batches:
            if not has_time(context, started):
                counts["deferred"] += len(batch)
                continue
            observed_at = utcnow()
            try:
                ids = [channel["platform_channel_id"] for channel in batch]
                items = platforms.youtube_channels(ids) if provider == "youtube" else platforms.twitch_users(ids)
            except Exception:
                # A failed profile batch was attempted; rotate it behind other work.
                store.mark_metadata_attempt([channel["id"] for channel in batch], observed_at)
                counts["failed"] += len(batch)
                LOG.exception("Channel metadata batch failed: provider=%s", provider)
                continue
            for channel in batch:
                if not has_time(context, started):
                    counts["deferred"] += 1
                    continue
                try:
                    # Deferred channels retain priority over completed work, including
                    # when per-channel follower lookups consume the remaining budget.
                    store.mark_metadata_attempt([channel["id"]], observed_at)
                    item = items.get(channel["platform_channel_id"])
                    if item is None:
                        counts["missing"] += 1
                        LOG.warning("Channel metadata unavailable: provider=%s channel_id=%s", provider, channel["id"])
                        continue
                    if item["id"] != channel["platform_channel_id"]:
                        raise ValueError("Platform returned a different channel")
                    if provider == "twitch":
                        item = {**item, "follower_count": platforms.twitch_follower_count(item["id"])}
                    metadata = youtube_channel(item) if provider == "youtube" else twitch_channel(item)
                    outcome = store.save_channel_snapshot(channel["id"], metadata, observed_at, collection_key)
                    counts[outcome] += 1
                except Exception:
                    counts["failed"] += 1
                    LOG.exception("Channel metadata update failed: provider=%s channel_id=%s", provider, channel["id"])
    LOG.info("Channel metadata refresh: %s", counts)
    if counts["failed"]:
        raise RuntimeError("Some channel updates failed; successful snapshots were committed")
    return counts
