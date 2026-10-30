BEGIN;

-- Keep the existing compound constraints: application upserts target them.
-- Add global uniqueness without merging rows or changing ownership/history.
-- Duplicate existing IDs abort this transaction for manual review.
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'channels'::regclass
          AND conname = 'channels_platform_channel_id_key'
    ) THEN
        ALTER TABLE channels
            ADD CONSTRAINT channels_platform_channel_id_key UNIQUE (platform_channel_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'videos'::regclass
          AND conname = 'videos_platform_video_id_key'
    ) THEN
        ALTER TABLE videos
            ADD CONSTRAINT videos_platform_video_id_key UNIQUE (platform_video_id);
    END IF;
END;
$migration$;

COMMIT;
