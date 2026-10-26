BEGIN;

ALTER TABLE channels ADD COLUMN IF NOT EXISTS is_tracked BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_stream_observed_at TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS lifecycle_observed_at TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS last_polled_at TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS ccv_sample_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS ccv_sample_sum BIGINT NOT NULL DEFAULT 0;

UPDATE videos v SET ccv_sample_count = s.samples, ccv_sample_sum = s.total,
    peak_ccv = s.peak, avg_ccv = round(s.total::numeric / s.samples)::integer
FROM (SELECT video_id, count(*) AS samples, sum(ccv) AS total, max(ccv) AS peak
      FROM ccv GROUP BY video_id) s
WHERE v.id = s.video_id;

CREATE TABLE IF NOT EXISTS channel_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id UUID NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1',
    callback_url TEXT NOT NULL,
    topic_url TEXT,
    desired BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'missing',
    provider_subscription_id TEXT,
    secret_version INTEGER,
    pending_secret_version INTEGER,
    pending_action TEXT CHECK (pending_action IN ('subscribe', 'unsubscribe')),
    pending_since TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    seeded_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    renew_at TIMESTAMPTZ,
    retry_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_id, event_type, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS channel_subscriptions_provider_id_idx
    ON channel_subscriptions (provider_subscription_id)
    WHERE provider_subscription_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS channel_subscriptions_due_idx
    ON channel_subscriptions (renew_at, retry_at);

CREATE TABLE IF NOT EXISTS processed_webhook_events (
    provider TEXT NOT NULL,
    event_key TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, event_key)
);

COMMIT;
