# Stream inactivity and missed notifications

`get-stream-stats` runs every two minutes. After polling both providers, it marks tracked `LIVE` videos `ENDED` when their last valid live observation is at least ten minutes old. Configure the threshold with `streams.inactivity_timeout_seconds` in [`config/runtime.yaml`](../config/runtime.yaml), currently `600`.

The sweep runs after API failures and deferred batches too. It runs on the next available poll after the threshold, normally within another two minutes. A stopped schedule, failed invocation before the sweep, unavailable database, or channel currently locked by another writer can delay it. This is a scheduled check rather than an exact ten-minute timer.

## What counts as data

`Store.upsert_video` advances `videos.last_live_observed_at` only for an accepted `LIVE` snapshot from a poll or the event worker's platform lookup. A valid live response with zero viewers or no viewer count still resets the timer. Stale snapshots, missing videos, API failures, and polling attempts do not. `last_polled_at` keeps its existing polling semantics and is not the inactivity clock.

`Store.expire_stale_streams` changes only tracked `LIVE` rows. Scheduled streams, confirmed ended history, untracked channels, and CCV samples/aggregates are preserved. The sweep locks channels before videos, skips channels being reconciled, and rechecks freshness before writing.

## Inferred ends and recovery

A timeout sets `ended_by_timeout = true` and estimates `actual_end_at` as `last_live_observed_at + inactivity_timeout_seconds`. This timestamp is an inference, not a platform-confirmed end. It leaves `lifecycle_observed_at` as the last authoritative snapshot and does not invent a poll or viewer sample. The poll summary reports `timed_out_streams`.

Twitch continues polling every tracked channel. `Store.pending_videos` also keeps timeout-ended YouTube videos in the polling queue until valid metadata returns. A newer `LIVE` snapshot reopens the same video UUID, clears the inferred end timestamp and flag, and resumes sampling. A confirmed end replaces the estimated end with the platform end time when available and clears the flag. A Twitch offline/replacement lookup likewise confirms the previous broadcast's end.

A provider outage longer than ten minutes can therefore temporarily mark an ongoing stream ended. Recovery is automatic once valid metadata returns. YouTube IDs that stay inaccessible remain in the recovery polling queue; they are not silently discarded. Confirmed-ended YouTube videos leave that queue. Historical CCV during an outage cannot be reconstructed by this timeout mechanism.

## Deployment

Apply [`004_stream_inactivity.sql`](../db/migrations/004_stream_inactivity.sql) to RDS **before deploying the updated Lambdas**, after any outstanding earlier migrations. Run the complete file in DBeaver, or use:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/004_stream_inactivity.sql
```

The migration adds `last_live_observed_at`, `ended_by_timeout`, and an index for live observation age. Existing live rows use the latest lifecycle observation or CCV sample as their initial heartbeat. Rows with neither get a ten-minute grace period starting at migration time. Old live rows with existing observations may time out on the first poll. Reapplying the migration preserves existing heartbeats and inferred ends.

Rebuild/deploy the Lambda ZIPs with the updated runtime YAML. No additional IAM permissions, secret, queue, or schedule is required. GitHub Actions deploys code and infrastructure but does not apply SQL migrations.

## Additional recovery strategies

Already implemented: two-minute polling reconciles known videos and all tracked Twitch channels; subscription jobs renew/reconcile registrations; event receipts prevent duplicate mutations; older observations cannot overwrite newer ones; delivery retries, a failure queue, and CloudWatch alarms surface processing failures. EventBridge retries and its DLQ protect events that reached AWS, not notifications a provider never delivered. See [AWS delivery retry behavior](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-rule-retry-policy.html).

The next useful addition is a periodic YouTube discovery sweep. Current YouTube polling checks known video IDs, and subscription setup seeds recent feed entries once. If the initial notification for a new video is missed, neither the poller nor this timeout can discover that unknown ID. Periodically scan recent channel feed/upload entries, batch-check discovered IDs through `videos.list`, and reconcile them through the same worker with a stable recovery event key. Bound the scan and request rate to the API budget. YouTube documents WebSub events for uploads and title/description changes, so they should not be treated as guaranteed stream lifecycle notifications; see [YouTube push notifications](https://developers.google.com/youtube/data/guides/push_notifications).

Also consider alarms for poll freshness, rising inferred-end counts, and subscription age/revocations, plus a controlled failure-queue replay tool. These are recommendations; this change adds the inactivity watchdog and automatic recovery of known streams.
