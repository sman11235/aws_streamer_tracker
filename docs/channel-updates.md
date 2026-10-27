# Channel metadata and history updater

`update-channels` refreshes tracked channels and records a history snapshot even when the values have not changed. Its default EventBridge Scheduler cron is `cron(0 * * * ? *)`: every hour at minute zero, UTC. Change `schedules.jobs.update-channel-history` in [`config/infrastructure.yaml`](../config/infrastructure.yaml) to change the schedule. It follows the existing `tracking_jobs_enabled` switch.

Before collecting metadata, every invocation discovers nonempty `streamer_info.youtube_channel_id` and `streamer_info.twitch_channel_id` values and creates missing `channels` rows linked to that streamer. New streamers and newly added platform IDs enter that invocation's refresh candidates; manually inserting a `channels` row is no longer necessary. The IDs drive discovery even if `has_youtube`/`has_twitch` have not yet been updated. Empty IDs are ignored, and surrounding spaces are removed.

Discovery is idempotent by platform/channel ID. Existing rows keep their owner, metadata, history, and explicit `is_tracked = false` setting. A streamer's display name is used as a temporary channel name until the platform responds. Existing tracked channels continue to refresh even if their ID is later removed or replaced in `streamer_info`; set their `channels.is_tracked` flag to false when collection should stop. Newly discovered tracked channels become eligible for subscription registration on the next subscription-manager run. The refresh summary includes a `discovered` count.

| Field | YouTube | Twitch with existing app credentials |
| --- | --- | --- |
| `channel_name` | Channel title | Display name |
| `profile_picture_url` | Preferred available thumbnail | Profile image |
| `description` | Channel description | Channel description |
| `subscriber_count` | Public API count; rounded, or null when hidden/unavailable | Supplied follower count; null when unavailable |
| `total_views` | Reported channel view count | Null; deprecated Get Users `view_count` is invalid |
| `video_count` | Reported public video count | Null; Get Users does not supply a video total |

The updater uses [YouTube Channels: list](https://developers.google.com/youtube/v3/docs/channels/list) with `snippet,statistics` and [Twitch Get Users](https://dev.twitch.tv/docs/api/reference/#get-users). YouTube counts retain the API's precision; missing/hidden counts remain null, while a reported zero is saved as zero. For Twitch, the shared `subscriber_count` field represents followers. `twitch_channel` reads an optional `follower_count` supplied alongside the user profile, validates it, and passes it to the current channel and history snapshot. Get Users does not return follower counts, and the current adapter does not enrich the response, so scheduled refreshes still store null unless a follower count is supplied upstream. The [Get Channel Followers](https://dev.twitch.tv/docs/api/reference/#get-channel-followers) endpoint is the separate source for that total.

## Deployment

Apply migrations in order from a machine with access to the private database. If migration 001 is already applied, only apply 002:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/002_channel_updates.sql
python3 scripts/build_lambda_zips.py
```

Then deploy the updated Terraform and ZIPs through the existing workflow. Apply the migration **before deploying** if tracking schedules are already enabled. The job uses the existing YouTube API-key and Twitch client-credential SSM parameters; no new secret is needed. Its role can read those parameters and the RDS secret. It has the existing logging, asynchronous retry, alarm, and failure-queue configuration.

Migration 002 adds `channels.description`, `channels.video_count`, and `channels.last_metadata_attempt_at`; adds `channel_history.description` and `channel_history.collection_key`; and adds refresh-order and per-run uniqueness indexes. Existing rows are preserved and the migration is repeatable. The base schema remains unchanged; new databases need the base schema followed by every numbered migration.

## Collection and recovery

The job batches YouTube IDs in groups of 50 and Twitch IDs in groups of 100, alternating providers. Endpoints, API parts, thumbnail preferences, and batch sizes are in [`config/runtime.yaml`](../config/runtime.yaml). An advisory lock prevents overlapping updater runs. Attempt timestamps let later invocations prioritize channels not recently attempted, including when missing/deleted channels are returned or the time budget is exhausted.

Each valid channel response updates current fields and `channels.last_updated`, and inserts matching `channel_history` values, in one transaction. History timestamps represent when the API observation began, not when the cron was scheduled. Older observations cannot overwrite newer metadata. A channel untracked while the request is in flight is skipped.

Scheduler includes its scheduled time in the payload. The updater turns it into a stable `collection_key`; retries skip channels already saved for that scheduled run. Successful channel transactions remain committed if another channel fails. A failed run raises an error to activate the configured retries. Missing channel results are counted and logged, leaving the previous metadata and history untouched. Deferred channels remain for the next invocation; check the logged summary when sizing larger imports.

For a manual refresh, invoke `update-channels` with `{}`. Each manual invocation creates a new collection key. To retry a particular scheduled run, replay its original `scheduled_at` payload. Failure destinations preserve that payload inside `requestPayload`; successful snapshots are not duplicated.
