"""PostgreSQL persistence. Network calls must happen outside atomic blocks."""

from contextlib import closing, contextmanager
from pathlib import Path
import ssl
from uuid import uuid4

from tracker.configuration import CONFIG, environment
from tracker.runtime import AWS_CLIENT


class Store:
    def __init__(self, connection):
        """Wrap a PostgreSQL connection and enable autocommit for ordinary queries.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            connection (pg8000.dbapi.Connection): Open PostgreSQL connection owned and
                eventually closed by this store.

        Returns:
            None: Store the connection and enable autocommit.
        """
        self.connection = connection
        self.connection.autocommit = True

    @classmethod
    def open(cls):
        """Open a PostgreSQL store using AWS credentials and configured TLS.

        Read credentials from Secrets Manager and resource addresses from the
        configured environment bindings. Verify the RDS certificate and hostname
        in verify-full mode; require mode retains encryption without verification.

        Args:
            cls (type[Store]): Store class whose constructor creates the connection wrapper.

        Returns:
            Store: New store owning a TLS connection authenticated with the RDS secret.
        """
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
        """Return this store when entering a connection-management with block.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.

        Returns:
            Store: This same instance, bound by the surrounding with statement.
        """
        return self

    def __exit__(self, *args):
        """Close the database connection and release its session advisory locks.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            *args (tuple[type[BaseException] | None, BaseException | None, types.TracebackType | None]):
                Context-manager exception type, exception value, and traceback; ignored
                during connection cleanup.

        Returns:
            None: Connection cleanup is complete; returning None does not suppress
                exceptions.
        """
        self.connection.close()

    def query(self, sql, values=()):
        """Execute parameterized SQL and return result rows as dictionaries.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            sql (str): SQL text containing positional %s placeholders for bound values.
            values (Sequence[Any]): Values to bind separately from SQL text; defaults to an
                empty tuple.

        Returns:
            list[dict[str, Any]]: Fetched rows keyed by column name, or [] when the
                statement has no result rows.
        """
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql, values)
            if cursor.description is None:
                return []
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    @contextmanager
    def atomic(self):
        """Yield a transaction that commits on success and rolls back on failure.

        Temporarily disable autocommit and restore it when leaving the block.
        Keep platform network requests outside this block to avoid long-lived locks.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.

        Returns:
            ContextManager[None]: Transaction context that commits on normal exit or rolls
                back on an exception.

        Yields:
            None: Control to the with block while its transaction is active.
        """
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
        """Try to acquire a named session advisory lock without waiting.

        Return False when another connection holds the lock. An acquired lock
        remains held until this store's connection closes.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            name (str): Stable job name hashed into a PostgreSQL session advisory-lock key.

        Returns:
            bool: True if this session acquired the lock; False if another session holds it.
        """
        return self.query("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (name,))[0]["locked"]

    def channels(self, platform=None):
        """Return tracked channels, optionally filtered by platform.

        Order channels with missing or older stream observations first so polling
        can prioritize channels that have waited longest.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            platform (str | None): YOUTUBE or TWITCH to filter channels, or None for both
                platforms.

        Returns:
            list[dict[str, Any]]: Tracked channel rows ordered by their last stream
                observation.
        """
        return self.query(
            """SELECT * FROM channels WHERE is_tracked AND (%s::text IS NULL OR platform::text = %s)
               ORDER BY last_stream_observed_at NULLS FIRST, id""",
            (platform, platform),
        )

    def discover_channels(self):
        """Create missing channel rows from IDs recorded in streamer_info.

        Use nonempty trimmed YouTube/Twitch IDs regardless of the legacy has_*
        flags. Link new rows to their streamer and use its name until metadata is
        available. Preserve existing ownership, tracking opt-outs, and history.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.

        Returns:
            int: Number of missing platform/channel pairs inserted from streamer_info.
        """
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
        """Return tracked platform channels not yet saved for collection_key.

        Exclude channels with an existing history snapshot for that run and order
        the remainder by their last metadata attempt, with unattempted rows first.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            platform (str): YOUTUBE or TWITCH platform whose metadata should be considered.
            collection_key (str): Refresh-run key used to exclude channels already saved for
                this run.

        Returns:
            list[dict[str, Any]]: Eligible tracked channel rows, with unattempted/older
                metadata attempts first.
        """
        return self.query(
            """SELECT c.* FROM channels c
               WHERE c.is_tracked AND c.platform::text = %s
                 AND NOT EXISTS (SELECT 1 FROM channel_history h
                                 WHERE h.channel_id = c.id AND h.collection_key = %s)
               ORDER BY c.last_metadata_attempt_at NULLS FIRST, c.id""",
            (platform, collection_key),
        )

    def mark_metadata_attempt(self, channel_ids, observed_at):
        """Record an attempt time for a batch of local channel UUIDs.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            channel_ids (Iterable[uuid.UUID | str]): Local channel UUIDs whose metadata
                attempt timestamps should be updated.
            observed_at (datetime.datetime): Timezone-aware start time of this metadata
                attempt.

        Returns:
            None: Attempt timestamps are updated in PostgreSQL.
        """
        self.query(
            """UPDATE channels SET last_metadata_attempt_at = %s
               WHERE id::text = ANY(%s::text[])""",
            (observed_at, [str(channel_id) for channel_id in channel_ids]),
        )

    def save_channel_snapshot(self, channel_id, metadata, observed_at, collection_key):
        """Commit current channel metadata and its history snapshot together.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            channel_id (uuid.UUID | str): Local UUID of the channel whose current metadata
                and history are updated.
            metadata (dict[str, str | int | None]): Normalized name, description, image,
                subscriber/video totals, and view total.
            observed_at (datetime.datetime): Timezone-aware API observation time used for
                history and stale-write protection.
            collection_key (str): Stable scheduled or manual run key used to deduplicate
                channel snapshots.

        Returns:
            str: Outcome: updated, duplicate, stale, or untracked; failures roll back both
                writes.
        """
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
        """Create a missing subscription or restore its desired tracking state.

        Keep an existing registration's callback, lease, and verification state.
        Uniqueness is determined by channel, event type, and subscription version.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            channel (Mapping[str, Any]): Tracked channel row containing its local id.
            event_type (str): Provider event kind, such as channel.feed or stream.online.
            callback (str): Public callback base URL without the local subscription query
                parameter.
            topic (str | None): YouTube feed topic URL, or None for subscriptions without a
                topic.

        Returns:
            None: A local subscription exists and its desired state is enabled.
        """
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
        """Synchronize desired flags from channel tracking and return joined rows.

        Filter by platform and order by modification time. This lookup also writes
        subscription desired state, allowing managers to remove untracked channels.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            platform (str): YOUTUBE or TWITCH platform whose subscriptions should be
                reconciled.

        Returns:
            list[dict[str, Any]]: Subscription rows joined with channel fields after desired
                flags are synchronized.
        """
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
        """Return one subscription joined with its channel, or None if unknown.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            subscription_id (str): Text form of the local subscription UUID supplied in a
                callback URL.

        Returns:
            dict[str, Any] | None: Joined subscription/channel row, or None when no matching
                record exists.
        """
        rows = self.query(
            """SELECT s.*, c.platform_channel_id, c.platform, c.is_tracked
               FROM channel_subscriptions s JOIN channels c ON c.id = s.channel_id
               WHERE s.id::text = %s""", (subscription_id,),
        )
        return rows[0] if rows else None

    def channel(self, channel_id, *, lock=False):
        """Return one channel, optionally locking it FOR UPDATE, or None if unknown.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            channel_id (uuid.UUID | str): Local channel UUID, converted to text for lookup.
            lock (bool): Whether to lock the matching row FOR UPDATE inside the caller
                transaction.

        Returns:
            dict[str, Any] | None: Matching channel row, or None if the channel does not
                exist.
        """
        rows = self.query(
            "SELECT * FROM channels WHERE id::text = %s" + (" FOR UPDATE" if lock else ""),
            (str(channel_id),),
        )
        return rows[0] if rows else None

    def update_subscription(self, subscription_id, **fields):
        """Update whitelisted subscription fields and its modification time.

        Bind all values as SQL parameters. Raise ValueError when no fields are
        provided or a field name is not in the supported update list.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            subscription_id (uuid.UUID | str): Local subscription UUID identifying the row
                to update.
            **fields (dict[str, Any]): Whitelisted subscription column names mapped to their
                new values.

        Returns:
            None: The requested fields and updated_at timestamp are written to PostgreSQL.
        """
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
        """Record Twitch's create response without undoing early verification.

        Preserve verified/enabled status when the matching remote subscription's
        callback committed before the original creation request returned.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            subscription_id (uuid.UUID | str): Local subscription UUID associated with the
                create request.
            provider_id (str): Remote Twitch EventSub subscription ID returned by the API.
            status (str): Remote status from the create response, which must not undo early
                verification.

        Returns:
            None: Remote identity and compatible local status are persisted.
        """
        # The verification callback may already have committed its result.
        self.query(
            """UPDATE channel_subscriptions SET
               status = CASE WHEN provider_subscription_id = %s AND status IN ('verified','enabled')
                             THEN status ELSE %s END,
               provider_subscription_id = %s, last_error = NULL, updated_at = now()
               WHERE id = %s""", (provider_id, status, provider_id, subscription_id),
        )

    def receipt(self, provider, event_key):
        """Insert a provider/event receipt and return whether it was new.

        Use inside the same atomic() block as event mutations so a failed update
        cannot leave a receipt that prevents a successful retry.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            provider (str): Provider namespace used to distinguish delivery IDs across
                platforms.
            event_key (str): Stable provider message ID or deterministic event fingerprint.

        Returns:
            bool: True for a newly inserted receipt; False when the event already has a
                receipt.
        """
        return bool(self.query(
            """INSERT INTO processed_webhook_events (provider, event_key)
               VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING event_key""", (provider, event_key),
        ))

    def processed(self, provider, event_key):
        """Return whether a receipt already exists for the provider and event key.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            provider (str): Provider namespace for the event receipt.
            event_key (str): Stable delivery ID or fingerprint whose receipt should be
                checked.

        Returns:
            bool: Whether this provider/event pair has a processed receipt.
        """
        return bool(self.query(
            "SELECT 1 FROM processed_webhook_events WHERE provider = %s AND event_key = %s",
            (provider, event_key),
        ))

    def pending_videos(self):
        """Return tracked live/scheduled YouTube videos, oldest polling attempts first.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.

        Returns:
            list[dict[str, Any]]: Known tracked YouTube LIVE/SCHEDULED video and channel
                identifiers in polling order.
        """
        return self.query(
            """SELECT v.platform_video_id, v.channel_id, c.platform_channel_id
               FROM videos v JOIN channels c ON c.id = v.channel_id
               WHERE c.is_tracked AND c.platform = 'YOUTUBE'
                 AND v.stream_status IN ('LIVE', 'SCHEDULED')
               ORDER BY v.last_polled_at NULLS FIRST, v.updated_at""",
        )

    def upsert_video(self, channel_id, video, observed_at):
        """Insert video metadata or update it when the observation is not older.

        Preserve existing lifecycle timestamps when incoming timestamps are absent.
        Return the local video UUID, or None when a newer saved observation prevents
        the update. This method does not create a CCV sample.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            channel_id (uuid.UUID | str): Local UUID of the channel owning the video.
            video (Mapping[str, Any]): Normalized video ID, title, status, optional
                metadata, and lifecycle timestamps.
            observed_at (datetime.datetime): Timezone-aware lookup start time used to reject
                older snapshots.

        Returns:
            uuid.UUID | None: Local video UUID for an accepted insert/update, or None for a
                stale observation.
        """
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
        """End live Twitch broadcasts other than the optional active broadcast ID.

        With no active_id, end every live broadcast for the channel. Record the
        observation time as the end time because no precise end timestamp is known.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            channel_id (uuid.UUID | str): Local channel UUID whose previous live broadcasts
                should be ended.
            observed_at (datetime.datetime): Timezone-aware observation time recorded as the
                approximate end time.
            active_id (str | None): Current Twitch broadcast ID to preserve, or None to end
                all live rows.

        Returns:
            None: Matching previous broadcasts are marked ENDED.
        """
        self.query(
            """UPDATE videos SET stream_status = 'ENDED', actual_end_at = %s
               WHERE channel_id = %s AND stream_status = 'LIVE'
               AND (%s::text IS NULL OR platform_video_id != %s)""",
            (observed_at, channel_id, active_id, active_id),
        )

    def sample(self, video_id, viewers, observed_at):
        """Insert one CCV sample and increment the video's running aggregates.

        Ignore an existing video/timestamp sample. For a new sample, update count,
        sum, peak, and rounded arithmetic mean without scanning history. Call within
        atomic() to keep the sample and aggregate updates in one transaction.

        Args:
            self (Store): Store instance owning the connection, transactions, and database
                operations.
            video_id (uuid.UUID | str): Local UUID of the video receiving the viewer sample.
            viewers (int): Observed nonnegative concurrent viewer count; zero is a valid
                observation.
            observed_at (datetime.datetime): Timezone-aware sample time forming the
                per-video deduplication key.

        Returns:
            None: A new sample and its aggregate changes are written, or a duplicate is
                ignored.
        """
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
