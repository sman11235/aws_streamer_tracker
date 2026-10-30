BEGIN;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS last_live_observed_at TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS ended_by_timeout BOOLEAN NOT NULL DEFAULT FALSE;

-- Poll attempts and updated_at can advance without any live data. Use actual
-- observations/samples when available; otherwise give legacy rows a fresh grace period.
UPDATE videos v
SET last_live_observed_at = COALESCE(
    GREATEST(v.lifecycle_observed_at,
             (SELECT max(c.observed_at) FROM ccv c WHERE c.video_id = v.id)),
    now()
)
WHERE v.stream_status = 'LIVE' AND v.last_live_observed_at IS NULL;

CREATE INDEX IF NOT EXISTS videos_live_observation_idx
    ON videos (last_live_observed_at, channel_id) WHERE stream_status = 'LIVE';

COMMIT;
