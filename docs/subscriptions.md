# WebSub and EventSub ingestion

The Lambda entrypoints use the shared `aws/lambda/tracker` package:

Runtime URLs, leases, retry intervals, limits, and environment bindings are in [`config/runtime.yaml`](../config/runtime.yaml). Schedules and infrastructure settings are in [`config/infrastructure.yaml`](../config/infrastructure.yaml). See the [configuration guide](configuration.md) for overrides and deployment requirements. Values described below are the checked-in defaults.

| Lambda | Responsibility |
| --- | --- |
| `subscribe-to-websub` | Subscribe tracked YouTube channels, renew due leases, seed recent feed entries, and unsubscribe untracked channels |
| `subscribe-to-eventsub` | Reconcile Twitch `stream.online` and `stream.offline` version 1 subscriptions without recreating healthy subscriptions |
| `handle-youtube-notifs` | Answer verification GETs, verify signed Atom POSTs, and publish video-change events |
| `handle-twitch-notifs` | Verify signed challenges, notifications, and revocations |
| `handle-stream-events` | Fetch authoritative platform metadata and apply idempotent stream updates |
| `get-stream-stats` | Poll Twitch channels and known live/scheduled YouTube videos every two minutes, recording available CCV samples |
| `update-channels` | Refresh channel profiles and public statistics hourly and record history snapshots |

Notifications flow through the existing EventBridge bus. Webhook handlers acknowledge deliveries only after EventBridge accepts them. The worker records the event receipt and database mutations in one transaction. Duplicate event IDs do not repeat mutations; late Twitch events recheck the currently active broadcast instead of blindly applying the old event's state.

## Deployment order

1. Apply the updated bootstrap from your authenticated local shell, using its existing state. The deployment role now needs SQS and CloudWatch permissions to create failure queues and alarms:

   ```sh
   terraform -chdir=terraform/bootstrap plan -out=bootstrap.tfplan
   terraform -chdir=terraform/bootstrap apply bootstrap.tfplan
   ```

2. Apply the additive migration with a PostgreSQL client that can reach the private RDS instance:

   ```sh
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/001_subscriptions.sql
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/002_channel_updates.sql
   ```

   For a new database, first apply `db/schema.sql`, then all numbered migrations in order. The migration adds tracking flags, lifecycle ordering, subscription records, processed-event receipts, and incremental CCV aggregates. It preserves existing application records and can be reapplied. Keep your database connection credentials local.

3. Create these SSM SecureString parameters, using the AWS-managed SSM encryption key unless you also grant the Lambda roles permission to decrypt your custom KMS key:

   | Parameter | Value |
   | --- | --- |
   | `/streamer-stats/youtube-websub-secret` | Stable random signing secret, less than 200 UTF-8 bytes |
   | `/streamer-stats/twitch-eventsub-secret` | Stable random signing secret, 10–100 ASCII characters |
   | `/streamer-stats/twitch-client-id` | Registered Twitch application's client ID |
   | `/streamer-stats/twitch-client-secret` | Registered Twitch application's client secret |
   | `/streamer-stats/youtube-api-key` | YouTube Data API v3 key, restricted to that API |

   A 64-character random hexadecimal string works for either signing secret. These are different values from the Twitch client secret. The parameter names are configurable Terraform variables. Never commit their values or put them in GitHub's plain configuration variables.

4. Push the implementation to `main` to deploy the callbacks, code, failure queue, and alarms. Leave the GitHub `dev` environment variable `TRACKING_JOBS_ENABLED` unset or `false` until the migration and all secrets are ready. All four schedules are disabled by default.

5. Add streamers and their actual YouTube channel IDs / numeric Twitch broadcaster IDs to `streamer_info`. The [channel updater](channel-updates.md) discovers those IDs and creates missing tracked `channels` rows on its next hourly run, or invoke `update-channels` with `{}` to discover them immediately. Subscription managers register tracked `channels` rows on their next run. Explicitly untracked existing channels remain disabled.

6. Test one real channel on each platform. You can manually invoke `subscribe-to-websub` and `subscribe-to-eventsub` with an empty JSON event, then inspect `channel_subscriptions`. Pending verification should become `enabled` for YouTube and `verified`, then `enabled` after reconciliation, for Twitch. Use the [Twitch CLI](https://dev.twitch.tv/docs/cli/event-command/) for additional signed callback tests.

7. Set `TRACKING_JOBS_ENABLED=true` in the GitHub `dev` environment and start a new deployment from `main`. Subscription maintenance runs every ten minutes; the stats poller runs every two minutes; channel/history updates run hourly UTC. Optional `ALARM_TOPIC_ARN` routes alarms to an existing SNS topic; without it, alarms are visible in CloudWatch but do not send notifications.

No migration, platform subscription request, or AWS deployment is performed by the build or test commands. CI tests use a separate PostgreSQL service and mocked platform responses.

## Subscription behavior

Each callback URL includes a nonsecret `subscription_id` query parameter so callbacks map to a specific local subscription. Subscription attempts are committed before making the platform request. Verification can arrive before that request returns, and the later API response preserves the verified state.

YouTube uses Google's [PubSubHubbub hub](https://pubsubhubbub.appspot.com/) and its 0.4 HMAC-SHA1 protocol. The implementation validates original request bytes before parsing XML and rejects unsafe XML constructs. Invalid HMACs are discarded with a 2xx acknowledgement as required by that protocol. A feed notification is a video change, not proof of a live broadcast. Only authoritative API metadata can classify a video as scheduled, live, or ended; ordinary uploads are ignored. Ambiguous or missing metadata is retried rather than inventing an ended/cancelled state.

The YouTube job runs every ten minutes. A subscription becomes due for renewal at the earlier of 80% of its actual granted lease or five days after its last successful verification. The five-day cap is `youtube.max_refresh_interval_seconds: 432000` in runtime YAML; it also applies to existing subscriptions whose stored renewal date is later. Accepting a renewal request does not reset the age: the verification callback does. Pending attempts are retried after ten minutes. The default requested lease is five days, so an ordinary five-day lease still renews after four days. Actual attempts happen on a maintenance invocation, subject to the job's batch/time limits and retry backoff; successful verification depends on the hub. If a hub grants very short leases, shorten the maintenance schedule accordingly. The initial feed seeds recent videos; it is not a complete historical backfill. Existing scheduled/live videos already in the database are also polled.

Twitch uses client-credentials app access tokens, HMAC-SHA256 over message ID + timestamp + original body, and a ten-minute freshness window with one minute of forward clock tolerance. Notifications are accepted only for the recorded channel, type, version, callback, and remote subscription ID. Healthy subscriptions survive token expiry; the API client obtains a new token as needed. Public stream lifecycle subscriptions do not grant access to a broadcaster's paid subscriber totals.

Transient failures persist retry times and bounded exponential backoff. Each manager performs at most 100 subscription attempts per invocation and stops near its Lambda deadline. Return summaries report deferred work; for large initial imports, manually invoke the manager again instead of waiting for the next scheduled run. The poller batches Twitch lookups in groups of 100 and YouTube lookups in groups of 50, alternating providers. Advisory locks prevent overlapping copies of each scheduled job; older Twitch observations and older snapshots of a particular YouTube video cannot overwrite newer state.

To stop tracking, set `channels.is_tracked = false` and run the maintenance jobs. They remove subscriptions and preserve historical videos and CCV samples. Deleting the channel directly also deletes its history through the original foreign-key cascades and loses subscription cleanup metadata.

For Twitch terminal failures (`authorization_revoked`, `user_removed`, `version_removed`), investigate the cause before retrying. After correcting it, reset the affected subscription status to `missing` and clear `retry_at`, or untrack/reconcile/retrack the channel. Other applications/environments' subscriptions are not deleted.

Keep signing secrets stable. Subscription rows record SSM versions, and callbacks can verify the active and pending versions during renewal. Changing the latest SSM version causes renewal/recreation on the next reconciliation. This is not a zero-interruption rotation mechanism; retain previous SSM versions while migration is in progress.

## Failures and recovery

`<project>-dev-ingestion-failures` retains failed events for 14 days. It receives EventBridge delivery failures, exhausted Lambda asynchronous invocations, and failed Scheduler deliveries. Inspect the message format before replay: EventBridge messages contain the event directly, while Lambda failure destinations wrap the original event in `requestPayload`. Fix the cause and manually invoke the appropriate function with the original payload. Receipts prevent repeated stream mutations.

CloudWatch alarms cover Lambda errors, webhook HTTP 5xx responses, and visible failure-queue messages. Subscription `status`, `last_error`, and `retry_at` provide per-channel diagnostics. Polling reports partial failures without discarding already-committed samples. Missing YouTube viewer counts are skipped rather than recorded as zero.

CCV averages are the arithmetic mean of successfully observed samples, not time-weighted averages. Twitch broadcast IDs are stored in `videos.platform_video_id`; they are not Twitch VOD IDs. The separate [channel updater](channel-updates.md) populates available public statistics and channel-history snapshots. The ingestion does not perform a complete YouTube historical backfill or infer cancellation from a temporarily missing public video.

## Build and test

```sh
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python scripts/build_lambda_zips.py
```

Set `TEST_DATABASE_HOST` and `TEST_DATABASE_PORT` to an isolated PostgreSQL 17 test server to run the integration tests; they create and delete random test schemas. Otherwise those tests are skipped. CI supplies this database and runs Python 3.12 to match Lambda.

The build config includes the shared package and `aws/lambda/requirements.lock`. The builder installs universal/ARM64 Python 3.12 wheels once and includes them in all Lambda ZIPs. It downloads the public RDS global CA bundle into each ZIP so database connections validate the RDS certificate and hostname. Builds need internet access; nothing is fetched from PyPI during Lambda invocation. ZIP metadata is normalized for reproducible builds. `--skip-dependencies` is only for local inspection and does not produce deployable artifacts.
