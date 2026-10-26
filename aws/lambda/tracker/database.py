"""PostgreSQL persistence. Network calls must happen outside atomic blocks."""

from contextlib import closing, contextmanager
from pathlib import Path
import ssl
from uuid import uuid4

from tracker.configuration import CONFIG, environment
from tracker.runtime import AWS_CLIENT


class Store:
    def __init__(self, connection):
        self.connection = connection
        self.connection.autocommit = True

    @classmethod
    def open(cls):
        import pg8000.dbapi

        secret = AWS_CLIENT.database_secret()
        settings = CONFIG["database"]
        tls = ssl.create_default_context(cafile=environment("database_ca_bundle", str(Path(__file__).with_name(settings["ca_bundle_filename"]))))
        # Match the existing libpq-style 'require' setting; verify-full uses the
        # bundled RDS certificate authorities and validates the hostname too.
        if settings["ssl_mode"] == "require":
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
        return cls(pg8000.dbapi.connect(
            host=environment("database_host"),
            port=int(environment("database_port", str(settings["port"]))),
            database=environment("database_name"),
            user=secret["username"], password=secret["password"],
            ssl_context=tls, timeout=settings["connect_timeout_seconds"],
        ))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def query(self, sql, values=()):
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql, values)
            if cursor.description is None:
                return []
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    @contextmanager
    def atomic(self):
        self.connection.autocommit = False
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self.connection.autocommit = True

    def lock_job(self, name):
        return self.query("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (name,))[0]["locked"]

    def channels(self, platform=None):
        return self.query(
            """SELECT * FROM channels WHERE is_tracked AND (%s::text IS NULL OR platform::text = %s)
               ORDER BY last_stream_observed_at NULLS FIRST, id""",
            (platform, platform),
        )

    def discover_channels(self):
        """Materialize streamer_info IDs without changing existing ownership/tracking."""
        rows = self.query(
            """INSERT INTO channels (vtuber_id, platform, platform_channel_id, channel_name)
               SELECT s.id, linked.platform::platform_type, btrim(linked.channel_id),
                      COALESCE(NULLIF(btrim(s.en_name), ''), NULLIF(btrim(s.jp_name), ''),
                               btrim(linked.channel_id))
               FROM streamer_info s
               CROSS JOIN LATERAL (VALUES
                   ('YOUTUBE', s.youtube_channel_id), ('TWITCH', s.twitch_channel_id)
               ) AS linked(platform, channel_id)
               WHERE NULLIF(btrim(linked.channel_id), '') IS NOT NULL
               ORDER BY s.id, linked.platform
               ON CONFLICT (platform, platform_channel_id) DO NOTHING
               RETURNING id""",
        )
        return len(rows)

    def channels_for_refresh(self, platform, collection_key):
        return self.query(
            """SELECT c.* FROM channels c
               WHERE c.is_tracked AND c.platform::text = %s
                 AND NOT EXISTS (SELECT 1 FROM channel_history h
                                 WHERE h.channel_id = c.id AND h.collection_key = %s)
               ORDER BY c.last_metadata_attempt_at NULLS FIRST, c.id""",
            (platform, collection_key),
        )

    def mark_metadata_attempt(self, channel_ids, observed_at):
        self.query(
            """UPDATE channels SET last_metadata_attempt_at = %s
               WHERE id::text = ANY(%s::text[])""",
            (observed_at, [str(channel_id) for channel_id in channel_ids]),
        )

    def save_channel_snapshot(self, channel_id, metadata, observed_at, collection_key):
        """Commit current metadata and one history row together, or neither."""
        with self.atomic():
            channel = self.channel(channel_id, lock=True)
            if not channel or not channel["is_tracked"]:
                return "untracked"
            if self.query(
                "SELECT 1 FROM channel_history WHERE channel_id = %s AND collection_key = %s",
                (channel_id, collection_key),
            ):
                return "duplicate"
            if channel["last_updated"] >= observed_at:
                return "stale"
            self.query(
                """INSERT INTO channel_history
                   (channel_id, observed_at, collection_key, channel_name, description,
                    profile_picture_url, subscriber_count, video_count, total_views)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (channel_id, observed_at, collection_key, metadata["channel_name"], metadata["description"],
                 metadata["profile_picture_url"], metadata["subscriber_count"], metadata["video_count"], metadata["total_views"]),
            )
            self.query(
                """UPDATE channels SET channel_name = %s, description = %s, profile_picture_url = %s,
                   subscriber_count = %s, video_count = %s, total_views = %s, last_updated = %s
                   WHERE id = %s""",
                (metadata["channel_name"], metadata["description"], metadata["profile_picture_url"],
                 metadata["subscriber_count"], metadata["video_count"], metadata["total_views"], observed_at, channel_id),
            )
            return "updated"

    def ensure_subscription(self, channel, event_type, callback, topic=None):
        self.query(
            """INSERT INTO channel_subscriptions
               (id, channel_id, event_type, callback_url, topic_url)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (channel_id, event_type, version) DO UPDATE
               SET desired = TRUE, updated_at = now()
               WHERE NOT channel_subscriptions.desired""",
            (str(uuid4()), channel["id"], event_type, callback, topic),
        )

    def subscriptions(self, platform):
        self.query(
            """UPDATE channel_subscriptions s SET desired = c.is_tracked
               FROM channels c WHERE s.channel_id = c.id AND c.platform::text = %s""", (platform,),
        )
        return self.query(
            """SELECT s.*, c.platform_channel_id, c.platform, c.is_tracked
               FROM channel_subscriptions s JOIN channels c ON c.id = s.channel_id
               WHERE c.platform::text = %s ORDER BY s.updated_at, s.id""", (platform,),
        )

    def subscription(self, subscription_id):
        rows = self.query(
            """SELECT s.*, c.platform_channel_id, c.platform, c.is_tracked
               FROM channel_subscriptions s JOIN channels c ON c.id = s.channel_id
               WHERE s.id::text = %s""", (subscription_id,),
        )
        return rows[0] if rows else None

    def channel(self, channel_id, *, lock=False):
        rows = self.query(
            "SELECT * FROM channels WHERE id::text = %s" + (" FOR UPDATE" if lock else ""),
            (str(channel_id),),
        )
        return rows[0] if rows else None

    def update_subscription(self, subscription_id, **fields):
        allowed = {
            "status", "provider_subscription_id", "secret_version", "pending_secret_version",
            "pending_action", "pending_since", "verified_at", "lease_expires_at", "renew_at",
            "retry_at", "last_error", "attempt_count", "callback_url", "topic_url", "seeded_at",
        }
        if not fields or fields.keys() - allowed:
            raise ValueError("Invalid subscription update")
        assignments = ", ".join(f"{name} = %s" for name in fields)
        self.query(
            f"UPDATE channel_subscriptions SET {assignments}, updated_at = now() WHERE id = %s",
            (*fields.values(), subscription_id),
        )

    def record_twitch_created(self, subscription_id, provider_id, status):
        # The verification callback may already have committed its result.
        self.query(
            """UPDATE channel_subscriptions SET
               status = CASE WHEN provider_subscription_id = %s AND status IN ('verified','enabled')
                             THEN status ELSE %s END,
               provider_subscription_id = %s, last_error = NULL, updated_at = now()
               WHERE id = %s""", (provider_id, status, provider_id, subscription_id),
        )

    def receipt(self, provider, event_key):
        return bool(self.query(
            """INSERT INTO processed_webhook_events (provider, event_key)
               VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING event_key""", (provider, event_key),
        ))

    def processed(self, provider, event_key):
        return bool(self.query(
            "SELECT 1 FROM processed_webhook_events WHERE provider = %s AND event_key = %s",
            (provider, event_key),
        ))

    def pending_videos(self):
        return self.query(
            """SELECT v.platform_video_id, v.channel_id, c.platform_channel_id
               FROM videos v JOIN channels c ON c.id = v.channel_id
               WHERE c.is_tracked AND c.platform = 'YOUTUBE'
                 AND v.stream_status IN ('LIVE', 'SCHEDULED')
               ORDER BY v.last_polled_at NULLS FIRST, v.updated_at""",
        )

    def upsert_video(self, channel_id, video, observed_at):
        rows = self.query(
            """INSERT INTO videos
               (channel_id, platform_video_id, title, description, thumbnail_url,
                stream_status, scheduled_start_at, actual_start_at, actual_end_at, lifecycle_observed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (channel_id, platform_video_id) DO UPDATE SET
                 title = EXCLUDED.title, description = EXCLUDED.description,
                 thumbnail_url = EXCLUDED.thumbnail_url, stream_status = EXCLUDED.stream_status,
                 scheduled_start_at = COALESCE(EXCLUDED.scheduled_start_at, videos.scheduled_start_at),
                 actual_start_at = COALESCE(EXCLUDED.actual_start_at, videos.actual_start_at),
                 actual_end_at = COALESCE(EXCLUDED.actual_end_at, videos.actual_end_at),
                 lifecycle_observed_at = EXCLUDED.lifecycle_observed_at
               WHERE videos.lifecycle_observed_at IS NULL OR videos.lifecycle_observed_at <= EXCLUDED.lifecycle_observed_at
               RETURNING id""",
            (channel_id, video["id"], video["title"], video.get("description"), video.get("thumbnail"),
             video["status"], video.get("scheduled"), video.get("started"), video.get("ended"), observed_at),
        )
        return rows[0]["id"] if rows else None

    def end_twitch_streams(self, channel_id, observed_at, active_id=None):
        self.query(
            """UPDATE videos SET stream_status = 'ENDED', actual_end_at = %s
               WHERE channel_id = %s AND stream_status = 'LIVE'
               AND (%s::text IS NULL OR platform_video_id != %s)""",
            (observed_at, channel_id, active_id, active_id),
        )

    def sample(self, video_id, viewers, observed_at):
        inserted = self.query(
            """INSERT INTO ccv (video_id, ccv, observed_at) VALUES (%s,%s,%s)
               ON CONFLICT DO NOTHING RETURNING video_id""",
            (video_id, viewers, observed_at),
        )
        if inserted:
            self.query(
                """UPDATE videos SET peak_ccv = greatest(coalesce(peak_ccv, 0), %s),
                   avg_ccv = round((ccv_sample_sum + %s)::numeric / (ccv_sample_count + 1))::integer,
                   ccv_sample_sum = ccv_sample_sum + %s, ccv_sample_count = ccv_sample_count + 1
                   WHERE id = %s""", (viewers, viewers, viewers, video_id),
            )
