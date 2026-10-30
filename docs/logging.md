# Job logging

All seven Lambda handlers use the `tracker` logger. The normal level is `INFO`, controlled by `logging.level` in [`config/runtime.yaml`](../config/runtime.yaml). Runtime YAML is packaged in each Lambda ZIP; rebuild and deploy after changing it.

Every invocation logs its deployed job name and AWS request ID at start, followed by a finish line containing elapsed milliseconds and its outcome. Manual local invocations use `request_id=local`. Overlapping scheduled jobs finish with `outcome=already running`. Unhandled failures log their exception class and propagate normally; webhook 5xx responses finish at `ERROR`, while rejected HTTP requests finish with their status and `outcome=rejected`.

| Job | Additional information at the normal log level |
| --- | --- |
| `subscribe-to-websub` | Selected channel/subscription counts; accepted subscribe/unsubscribe requests; seeded event count; healthy, retry, verification, and inactive skips; failed/deferred counts |
| `subscribe-to-eventsub` | Selected local subscriptions; total/enabled remote registrations at lookup time; created/enabled/removed counts; retry/verification waits, create conflicts, and terminal registrations needing intervention |
| `get-stream-stats` | Twitch channels, YouTube videos, and batch counts; snapshot outcomes; missing/non-stream YouTube responses; failed/deferred batches; inactivity endings |
| `update-channels` | Collection key; newly discovered channels; candidate counts for each provider; final update/missing/failure/deferred counts |
| `handle-stream-events` | Provider, event kind, local channel/video IDs, resolved stream status, and reconciliation outcome |
| `handle-youtube-notifs` | Verified/denied subscriptions, accepted notification event counts, invalid signatures, and malformed or mismatched requests |
| `handle-twitch-notifs` | Verified/revoked subscriptions, accepted event types, stale deliveries, invalid signatures, and mismatched subscriptions |

Subscription failures include the local subscription/channel IDs, event type, HTTP status when available, attempt number, and retry delay. Existing detailed batch failure traces remain available. Routine successful channel updates and individual subscription/batch attempts are `DEBUG`; no per-viewer-sample success log is emitted.

The EventSub `remote_total` and `remote_enabled` values describe the API listing for the Twitch application, which may include other environments. Final `created` and `enabled` counters describe only this invocation's work, not the complete inventory. A deferred subscription may already be remotely enabled. Accepted WebSub requests are not proof of successful verification; the callback logs that separately.

Examples:

```text
Job started: job=get-stream-stats request_id=<aws-request-id>
Stream polling work selected: twitch_channels=329 youtube_videos=12 twitch_batches=4 youtube_batches=1 inactivity_timeout_seconds=600
Job finished: job=get-stream-stats request_id=<aws-request-id> elapsed_ms=1240 outcome=completed http_status=None
```

Search the Lambda's CloudWatch log group for `Job failed`, `Polling batch failed`, `Stream inactivity timeout`, `Subscription attempt failed`, or a local channel/subscription ID. The existing final summaries show whether work was deferred because of invocation budget or subscription attempt limits. An empty workload still logs a selection count and a completion.

New logs do not dump events, HTTP bodies/headers, challenge responses, callback URLs, API keys, tokens, or signing/database secrets. Invocation failure logs record exception classes rather than exception text. Set the level to `DEBUG` temporarily when individual subscription/channel attempts are needed; it produces substantially more output.

Logging itself requires no additional AWS permissions or migration. The separate inactivity behavior still requires migration 004 before its updated code is deployed.
