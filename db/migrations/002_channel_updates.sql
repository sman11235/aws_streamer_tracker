BEGIN;

ALTER TABLE channels ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS video_count BIGINT;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_metadata_attempt_at TIMESTAMPTZ;
ALTER TABLE channel_history ADD COLUMN IF NOT EXISTS description TEXT;
-- A scheduled run keeps this key across retries, while observed_at records
-- the actual API observation time rather than backdating data to the schedule.
ALTER TABLE channel_history ADD COLUMN IF NOT EXISTS collection_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS channel_history_collection_key_idx
    ON channel_history (channel_id, collection_key)
    WHERE collection_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS channels_metadata_due_idx
    ON channels (last_metadata_attempt_at, id) WHERE is_tracked;

COMMIT;
