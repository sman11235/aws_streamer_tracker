CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE gender_type AS ENUM ('M', 'F', 'NB');
CREATE TYPE platform_type AS ENUM ('TWITCH', 'YOUTUBE');
CREATE TYPE stream_status_type AS ENUM ('LIVE', 'ENDED', 'SCHEDULED', 'CANCELLED');

CREATE TABLE streamer_info (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  jp_name TEXT NOT NULL,
  en_name TEXT NOT NULL,
  group_affiliation TEXT,
  generation_affiliation TEXT,
  gender gender_type NOT NULL,
  main_platform platform_type NOT NULL,
  has_twitch BOOLEAN NOT NULL DEFAULT FALSE,
  has_youtube BOOLEAN NOT NULL DEFAULT FALSE,
  twitch_channel_id TEXT,
  youtube_channel_id TEXT,
  lang TEXT NOT NULL
);

CREATE TABLE channels (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  vtuber_id UUID NOT NULL REFERENCES streamer_info(id) ON DELETE CASCADE,
  platform platform_type NOT NULL,
  platform_channel_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  profile_picture_url TEXT,
  subscriber_count BIGINT,
  total_views BIGINT,
  last_updated TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (platform, platform_channel_id)
);

CREATE TABLE videos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  channel_id UUID NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  platform_video_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  thumbnail_url TEXT,
  stream_status stream_status_type NOT NULL,
  scheduled_start_at TIMESTAMPTZ,
  actual_start_at TIMESTAMPTZ,
  actual_end_at TIMESTAMPTZ,
  peak_ccv INTEGER,
  avg_ccv INTEGER,
  is_members_only BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel_id, platform_video_id)
);

CREATE TABLE ccv (
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  ccv INTEGER NOT NULL CHECK (ccv >= 0),
  observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (video_id, observed_at)
);

CREATE TABLE channel_history (
  channel_id UUID NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  observed_at TIMESTAMPTZ NOT NULL,
  channel_name TEXT NOT NULL,
  profile_picture_url TEXT,
  subscriber_count BIGINT,
  video_count BIGINT,
  total_views BIGINT,
  PRIMARY KEY (channel_id, observed_at)
);

CREATE INDEX channels_vtuber_id_idx ON channels (vtuber_id);
CREATE INDEX videos_channel_id_idx ON videos (channel_id);
CREATE INDEX videos_stream_status_idx ON videos (stream_status);
CREATE INDEX ccv_observed_at_idx ON ccv (observed_at);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER videos_set_updated_at
BEFORE UPDATE ON videos
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
