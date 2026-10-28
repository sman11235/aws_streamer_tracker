"""Reconcile authoritative platform snapshots and persist CCV samples."""

from itertools import zip_longest
import time

from tracker.configuration import CONFIG
from tracker.database import Store
from tracker.platforms import PLATFORMS
from tracker.runtime import LOG, has_time, utcnow


def youtube_video(item):
    """Normalize YouTube metadata into a live-stream snapshot, or None for uploads.

    Give actual end times precedence over stale live flags. Preserve unknown
    viewer counts as None. Absence of statistics.viewCount marks a stream as
    members-only; zero counts as present.
    This is an application heuristic, not a documented YouTube access guarantee.
    Raise RuntimeError when lifecycle metadata is inconsistent so later polling
    can retry without inventing a cancelled state.

    Args:
        item (dict[str, Any]): YouTube video resource containing snippet and optional
            liveStreamingDetails and statistics. Requests must include statistics
            so field omission can be used for membership classification.

    Returns:
        dict[str, str | int | bool | None] | None: Normalized stream metadata including
            is_members_only, or None for an ordinary non-stream upload.
    """
    snippet = item["snippet"]
    details = item.get("liveStreamingDetails", {})
    if not details:
        return None  # Ordinary uploads are not live streams.
    if details.get("actualEndTime"):
        status = "ENDED"
    elif snippet.get("liveBroadcastContent") == "live":
        status = "LIVE"
    elif details.get("scheduledStartTime") and not details.get("actualStartTime"):
        status = "SCHEDULED"
    else:
        # Avoid inventing an ended/cancelled state while the public API is
        # temporarily incomplete. A later poll or delivery will reconcile it.
        raise RuntimeError("YouTube lifecycle metadata is not yet consistent")
    thumbnails = snippet.get("thumbnails", {})
    thumbnail = next((thumbnails[size]["url"] for size in CONFIG["youtube"]["thumbnail_priority"] if size in thumbnails), None)
    viewers = details.get("concurrentViewers")
    return {
        "id": item["id"], "title": snippet["title"], "description": snippet.get("description"),
        "thumbnail": thumbnail, "status": status,
        "scheduled": details.get("scheduledStartTime"), "started": details.get("actualStartTime"),
        "ended": details.get("actualEndTime"),
        "viewers": int(viewers) if viewers is not None and status == "LIVE" else None,
        "is_members_only": "viewCount" not in item.get("statistics", {}),
    }


def twitch_video(item):
    """Normalize a current Twitch broadcast, thumbnail, start time, and CCV.

    Use the broadcast ID as the stream identifier; it is not a Twitch VOD ID.

    Args:
        item (dict[str, Any]): Current Twitch stream object with broadcast ID, title,
            start time, and viewer count.

    Returns:
        dict[str, str | int]: Normalized LIVE broadcast metadata and concurrent viewer
            count.
    """
    return {
        # Twitch stream IDs identify broadcasts; they are not Twitch VOD IDs.
        "id": item["id"], "title": item["title"], "status": "LIVE", "started": item["started_at"],
        "thumbnail": item.get("thumbnail_url", "").replace("{width}", str(CONFIG["twitch"]["thumbnail_width"])).replace("{height}", str(CONFIG["twitch"]["thumbnail_height"])),
        "viewers": int(item["viewer_count"]),
    }


def apply_snapshot(store, channel, observed_at, video, *, provider, event_key=None, sample=False):
    """Apply a platform observation and optional sample in one transaction.

    Lock the channel, preserve newer observations, and reconcile old Twitch
    broadcasts before updating the active video. Return applied, duplicate,
    untracked, or stale as appropriate for the channel observation.

    Args:
        store (Store): Database adapter used for transactions, channel locking, and
            persistence.
        channel (Mapping[str, Any]): Mapping containing the local channel id to
            reconcile.
        observed_at (datetime.datetime): Timezone-aware API lookup start time used for
            observation ordering.
        video (Mapping[str, Any] | None): Normalized stream metadata, or None when no
            current broadcast exists.
        provider (str): youtube or twitch, selecting the appropriate lifecycle ordering
            rules.
        event_key (str | None): Optional delivery identity to deduplicate in the same
            transaction as mutations.
        sample (bool): Whether this observation came from polling. Record its polling
            time and create a CCV sample when a viewer count is available.

    Returns:
        str: Processing outcome: applied, duplicate, untracked, or stale.
    """
    polled_at = observed_at if sample else None
    with store.atomic():
        current = store.channel(channel["id"], lock=True)
        if event_key and not store.receipt(provider, event_key):
            return "duplicate"
        if not current or not current["is_tracked"]:
            return "untracked"
        if provider == "twitch":
            if current["last_stream_observed_at"] and current["last_stream_observed_at"] >= observed_at:
                return "stale"
            store.end_twitch_streams(channel["id"], observed_at, video["id"] if video else None,
                                    polled_at=polled_at)
            store.query("UPDATE channels SET last_stream_observed_at = %s WHERE id = %s", (observed_at, channel["id"]))
        if video:
            video_id = store.upsert_video(channel["id"], video, observed_at, polled_at=polled_at)
            if sample and video_id and video.get("viewers") is not None:
                store.sample(video_id, video["viewers"], observed_at)
        return "applied"


def handle(event, context, *, store_factory=Store.open, platforms=PLATFORMS):
    """Process one EventBridge lifecycle event using current platform metadata.

    Validate the source/type/provider, skip processed or untracked events, and
    fetch authoritative metadata before applying transactional changes. Delayed
    Twitch offline events therefore preserve a newer active broadcast. Do not
    collect viewer samples here; the scheduled poller owns sample collection.

    Args:
        event (dict[str, Any]): EventBridge envelope containing the lifecycle source,
            detail-type, and normalized detail.
        context (LambdaContext | None): Lambda invocation context; accepted for the
            handler interface and otherwise unused.
        store_factory (Callable[[], ContextManager[Store]]): Factory opening a
            context-managed store for this delivery.
        platforms (Platforms): Injectable API client used to fetch authoritative current
            video/stream metadata.

    Returns:
        dict[str, str]: A status mapping identifying an applied, duplicate, untracked,
            or stale event.

    Raises:
        ValueError: Event routing or channel ownership is invalid.
        RuntimeError: Required platform metadata is not yet available.
    """
    detail = event["detail"]
    provider = detail["provider"]
    kinds = {"youtube": {"video.changed"}, "twitch": {"stream.online", "stream.offline"}}
    if (
        provider not in kinds or detail["kind"] not in kinds[provider]
        or event.get("source") != CONFIG["events"]["source_prefix"] + provider
        or event.get("detail-type") != CONFIG["events"]["detail_type"]
    ):
        raise ValueError("Unexpected stream event")
    with store_factory() as store:
        if store.processed(provider, detail["event_key"]):
            return {"status": "duplicate"}
        channel = store.channel(detail["channel_id"])
        if not channel or not channel["is_tracked"]:
            return {"status": "untracked"}
        if str(channel["platform"]).lower() != provider:
            raise ValueError("Event platform does not match the channel")
        observed_at = utcnow()
        if provider == "youtube":
            item = platforms.youtube_videos([detail["video_id"]]).get(detail["video_id"])
            if not item:
                raise RuntimeError("YouTube video metadata is not available yet")
            if item["snippet"]["channelId"] != channel["platform_channel_id"]:
                raise ValueError("YouTube video belongs to another channel")
            video = youtube_video(item)
        else:
            item = platforms.twitch_streams([channel["platform_channel_id"]]).get(channel["platform_channel_id"])
            if not item and detail["kind"] == "stream.online":
                raise RuntimeError("Twitch stream metadata is not available yet")
            video = twitch_video(item) if item else None
        status = apply_snapshot(store, channel, observed_at, video, provider=provider, event_key=detail["event_key"])
        return {"status": status}


def chunks(items, size):
    """Return consecutive sequence slices containing at most size items each.

    Args:
        items (Sequence[T]): Sliceable input sequence; T represents the type of each
            input element.
        size (int): Positive maximum number of elements in each returned slice.

    Returns:
        list[Sequence[T]]: Consecutive input slices; an empty input produces an empty
            list.
    """
    return [items[index:index + size] for index in range(0, len(items), size)]


def poll(event, context, *, store_factory=Store.open, platforms=PLATFORMS):
    """Poll tracked Twitch channels and known live/scheduled YouTube videos.

    Batch requests within configured API limits, alternate providers, and defer
    work near the invocation deadline. Reconcile lifecycle state and save known
    viewer counts transactionally. Keep successful batches when another fails.

    Args:
        event (dict[str, Any]): Scheduler payload; no fields are required by this
            poller.
        context (LambdaContext | None): Invocation context used to reserve completion
            time, or None for the local budget.
        store_factory (Callable[[], ContextManager[Store]]): Factory opening a
            context-managed store for polling work.
        platforms (Platforms): Injectable API client providing batched current streams
            and video metadata.

    Returns:
        dict[str, int | str]: Processed/failed/deferred counters, or a skipped reason
            for an overlapping job.

    Raises:
        RuntimeError: At least one polling batch failed and requires a retry.
    """
    started = time.monotonic()
    result = {"sampled_channels": 0, "failed_batches": 0, "deferred_batches": 0}
    with store_factory() as store:
        if not store.lock_job("get-stream-stats"):
            return {"skipped": "already running"}
        twitch_batches = chunks(store.channels("TWITCH"), CONFIG["twitch"]["polling_batch_size"])
        youtube_batches = chunks(store.pending_videos(), CONFIG["youtube"]["polling_batch_size"])
        # Alternate providers so a large Twitch backlog cannot starve YouTube.
        batches = [(kind, batch) for pair in zip_longest(twitch_batches, youtube_batches)
                   for kind, batch in zip(("twitch", "youtube"), pair) if batch]
        for provider, batch in batches:
            if not has_time(context, started):
                result["deferred_batches"] += 1
                continue
            try:
                observed_at = utcnow()
                if provider == "twitch":
                    streams = platforms.twitch_streams([channel["platform_channel_id"] for channel in batch])
                    for channel in batch:
                        item = streams.get(channel["platform_channel_id"])
                        apply_snapshot(store, channel, observed_at, twitch_video(item) if item else None,
                                       provider=provider, sample=True)
                        result["sampled_channels"] += 1
                else:
                    videos = platforms.youtube_videos([video["platform_video_id"] for video in batch])
                    for row in batch:
                        store.query(
                            "UPDATE videos SET last_polled_at = %s WHERE channel_id = %s AND platform_video_id = %s",
                            (observed_at, row["channel_id"], row["platform_video_id"]),
                        )
                        item = videos.get(row["platform_video_id"])
                        if not item:
                            continue
                        if item["snippet"]["channelId"] != row["platform_channel_id"]:
                            raise ValueError("YouTube video belongs to another channel")
                        apply_snapshot(store, {"id": row["channel_id"]}, observed_at, youtube_video(item),
                                       provider=provider, sample=True)
                        result["sampled_channels"] += 1
            except Exception:
                result["failed_batches"] += 1
                LOG.exception("Polling batch failed: provider=%s", provider)
    LOG.info("Stream polling: %s", result)
    if result["failed_batches"]:
        raise RuntimeError("Some polling batches failed; successful samples were committed")
    return result
