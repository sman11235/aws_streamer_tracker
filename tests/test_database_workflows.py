"""Real PostgreSQL tests. Set TEST_DATABASE_HOST/PORT to an isolated test server."""

from datetime import timedelta
from contextlib import closing
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from tests.support import SECRET, api_event, aws_mock, feed, twitch_event, youtube_channel_item
from tracker.channels import update as update_channels, youtube_channel
from tracker.database import Store
from tracker.platforms import callback_url
from tracker.runtime import utcnow
from tracker.streams import apply_snapshot, handle, poll
from tracker.subscriptions import eventsub, websub
from tracker.webhooks import twitch, youtube


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_HOST"), "Set TEST_DATABASE_HOST for PostgreSQL integration tests")
class DatabaseWorkflowTests(unittest.TestCase):
    def connect(self):
        import pg8000.dbapi
        connection = pg8000.dbapi.connect(
            host=os.environ["TEST_DATABASE_HOST"], port=int(os.environ.get("TEST_DATABASE_PORT", "5432")),
            user="postgres", database="postgres", password=os.environ.get("TEST_DATABASE_PASSWORD"),
            ssl_context=False, timeout=5,
        )
        connection.autocommit = True
        with closing(connection.cursor()) as cursor:
            cursor.execute(f'SET search_path TO "{self.schema}", public')
        return connection

    def open_store(self):
        return Store(self.connect())

    def setUp(self):
        self.schema = "test_" + uuid4().hex
        self.store = self.open_store()
        self.store.query("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
        self.store.query(f'CREATE SCHEMA "{self.schema}"')
        self.store.query((ROOT / "db/schema.sql").read_text())
        self.migrate()
        self.vtuber = self.store.query(
            """INSERT INTO streamer_info (jp_name,en_name,gender,main_platform,lang)
               VALUES ('test','test','NB','TWITCH','en') RETURNING id""",
        )[0]["id"]
        self.aws = aws_mock()

    def tearDown(self):
        self.store.query("SET search_path TO public")
        self.store.query(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.store.connection.close()

    def migrate(self):
        for migration in sorted((ROOT / "db/migrations").glob("*.sql")):
            self.store.query(migration.read_text())

    def channel(self, platform="TWITCH", platform_id=None):
        return self.store.query(
            """INSERT INTO channels (vtuber_id,platform,platform_channel_id,channel_name)
               VALUES (%s,%s,%s,'test') RETURNING *""",
            (self.vtuber, platform, platform_id or ("1234" if platform == "TWITCH" else "UC-channel")),
        )[0]

    def test_refresh_discovers_new_streamer_channels_and_records_history(self):
        # No channels rows and the has_* flags retain their default false values.
        self.store.query(
            """INSERT INTO streamer_info
               (jp_name,en_name,gender,main_platform,lang,youtube_channel_id,twitch_channel_id)
               VALUES ('new','New streamer','NB','YOUTUBE','en',' UC-new ', ' 5678 ')
               RETURNING id""",
        )
        platforms = MagicMock()
        platforms.youtube_channels.return_value = {"UC-new": youtube_channel_item("UC-new")}
        platforms.twitch_users.return_value = {"5678": {"id": "5678", "display_name": "New Twitch"}}
        result = update_channels({}, None, store_factory=self.open_store, platforms=platforms)
        self.assertEqual((result["discovered"], result["updated"]), (2, 2))
        platforms.youtube_channels.assert_called_once_with(["UC-new"])
        platforms.twitch_users.assert_called_once_with(["5678"])
        rows = self.store.query("SELECT platform_channel_id, channel_name FROM channels ORDER BY platform_channel_id")
        self.assertEqual(rows, [{"platform_channel_id": "5678", "channel_name": "New Twitch"},
                                {"platform_channel_id": "UC-new", "channel_name": "Updated channel"}])
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channel_history")[0]["n"], 2)

    def test_existing_streamer_new_platform_is_discovered_on_scheduled_retry(self):
        self.store.query("UPDATE streamer_info SET youtube_channel_id = 'UC-channel' WHERE id = %s", (self.vtuber,))
        platforms = MagicMock()
        platforms.youtube_channels.return_value = {"UC-channel": youtube_channel_item()}
        platforms.twitch_users.return_value = {"1234": {"id": "1234", "display_name": "Added Twitch"}}
        event = {"scheduled_at": utcnow().isoformat()}
        first = update_channels(event, None, store_factory=self.open_store, platforms=platforms)
        original = self.store.query("SELECT * FROM channels")[0]
        self.assertEqual((first["discovered"], first["updated"]), (1, 1))
        self.store.query("UPDATE streamer_info SET twitch_channel_id = '1234' WHERE id = %s", (self.vtuber,))
        second = update_channels(event, None, store_factory=self.open_store, platforms=platforms)
        self.assertEqual((second["discovered"], second["updated"]), (1, 1))
        platforms.youtube_channels.assert_called_once()
        platforms.twitch_users.assert_called_once_with(["1234"])
        self.assertEqual(self.store.channel(original["id"])["platform_channel_id"], "UC-channel")
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channel_history")[0]["n"], 2)
        self.assertEqual(self.store.discover_channels(), 0)

    def test_discovery_preserves_explicitly_untracked_channels(self):
        channel = self.channel("YOUTUBE")
        self.store.query("UPDATE channels SET is_tracked = FALSE WHERE id = %s", (channel["id"],))
        self.store.query("UPDATE streamer_info SET youtube_channel_id = 'UC-channel' WHERE id = %s", (self.vtuber,))
        platforms = MagicMock()
        result = update_channels({}, None, store_factory=self.open_store, platforms=platforms)
        self.assertEqual((result["discovered"], result["updated"]), (0, 0))
        self.assertFalse(self.store.channel(channel["id"])["is_tracked"])
        platforms.youtube_channels.assert_not_called()

    def test_discovery_ignores_empty_ids_and_uses_streamer_name_until_metadata_arrives(self):
        self.store.query(
            "UPDATE streamer_info SET youtube_channel_id = '   ', twitch_channel_id = NULL, has_youtube = TRUE WHERE id = %s",
            (self.vtuber,),
        )
        self.assertEqual(self.store.discover_channels(), 0)
        self.store.query("UPDATE streamer_info SET twitch_channel_id = '1234' WHERE id = %s", (self.vtuber,))
        self.assertEqual(self.store.discover_channels(), 1)
        row = self.store.query("SELECT * FROM channels")[0]
        self.assertEqual((str(row["vtuber_id"]), row["channel_name"], row["platform_channel_id"]),
                         (str(self.vtuber), "test", "1234"))
        self.assertTrue(row["is_tracked"])

    def test_discovery_does_not_reassign_existing_channel_ownership(self):
        channel = self.channel("YOUTUBE")
        self.store.query(
            """INSERT INTO streamer_info
               (jp_name,en_name,gender,main_platform,lang,youtube_channel_id)
               VALUES ('other','Other streamer','NB','YOUTUBE','en','UC-channel')""",
        )
        self.assertEqual(self.store.discover_channels(), 0)
        self.assertEqual(str(self.store.channel(channel["id"])["vtuber_id"]), str(self.vtuber))
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channels")[0]["n"], 1)

    def test_discovery_keeps_previous_channel_history_when_streamer_adds_new_id(self):
        channel = self.channel("YOUTUBE")
        self.store.save_channel_snapshot(channel["id"], youtube_channel(youtube_channel_item()), utcnow(), "old")
        self.store.query("UPDATE streamer_info SET youtube_channel_id = 'UC-new' WHERE id = %s", (self.vtuber,))
        self.assertEqual(self.store.discover_channels(), 1)
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channels")[0]["n"], 2)
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channel_history WHERE channel_id = %s", (channel["id"],))[0]["n"], 1)

    def test_channel_metadata_and_history_are_updated_together(self):
        channel = self.channel("YOUTUBE")
        observed = utcnow()
        metadata = youtube_channel(youtube_channel_item())
        self.assertEqual(self.store.save_channel_snapshot(channel["id"], metadata, observed, "run-1"), "updated")
        current = self.store.channel(channel["id"])
        history = self.store.query("SELECT * FROM channel_history")[0]
        for key, expected in metadata.items():
            self.assertEqual(current[key], expected)
            self.assertEqual(history[key], expected)
        self.assertEqual(current["last_updated"], history["observed_at"])

    def test_channel_history_rolls_back_when_current_update_fails(self):
        channel = self.channel("YOUTUBE")
        original = self.store.query

        def query(sql, values=()):
            if "UPDATE channels SET channel_name" in sql:
                raise RuntimeError("simulated update failure")
            return original(sql, values)

        with patch.object(self.store, "query", side_effect=query), self.assertRaises(RuntimeError):
            self.store.save_channel_snapshot(channel["id"], youtube_channel(youtube_channel_item()), utcnow(), "run-1")
        self.assertEqual(self.store.channel(channel["id"])["channel_name"], "test")
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channel_history")[0]["n"], 0)

    def test_scheduled_channel_retry_skips_completed_channels(self):
        channel = self.channel("YOUTUBE")
        platforms = MagicMock()
        platforms.youtube_channels.return_value = {"UC-channel": youtube_channel_item()}
        scheduled_at = utcnow() - timedelta(hours=2)
        event = {"scheduled_at": scheduled_at.isoformat()}
        self.assertEqual(update_channels(event, None, store_factory=self.open_store, platforms=platforms)["updated"], 1)
        self.assertEqual(update_channels(event, None, store_factory=self.open_store, platforms=platforms)["updated"], 0)
        platforms.youtube_channels.assert_called_once()
        history = self.store.query("SELECT * FROM channel_history")
        self.assertEqual(len(history), 1)
        self.assertGreater(history[0]["observed_at"], scheduled_at)
        self.assertEqual(self.store.channel(channel["id"])["last_updated"], history[0]["observed_at"])

    def test_distinct_channel_runs_record_history_even_when_unchanged(self):
        channel = self.channel("YOUTUBE")
        metadata = youtube_channel(youtube_channel_item())
        first = utcnow()
        self.store.save_channel_snapshot(channel["id"], metadata, first, "run-1")
        self.store.save_channel_snapshot(channel["id"], metadata, first + timedelta(seconds=1), "run-2")
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channel_history")[0]["n"], 2)
        self.migrate()
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channel_history")[0]["n"], 2)

    def test_late_and_untracked_channel_snapshots_do_not_write_history(self):
        channel = self.channel("YOUTUBE")
        metadata = youtube_channel(youtube_channel_item())
        now = utcnow()
        self.store.save_channel_snapshot(channel["id"], metadata, now, "new")
        self.assertEqual(self.store.save_channel_snapshot(channel["id"], metadata, now - timedelta(minutes=1), "old"), "stale")
        self.store.query("UPDATE channels SET is_tracked = FALSE WHERE id = %s", (channel["id"],))
        self.assertEqual(self.store.save_channel_snapshot(channel["id"], metadata, now + timedelta(minutes=1), "untracked"), "untracked")
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM channel_history")[0]["n"], 1)

    def test_missing_channel_does_not_starve_unattempted_channels(self):
        first = self.channel("YOUTUBE")
        second = self.channel("YOUTUBE", "UC-second")
        self.store.mark_metadata_attempt([first["id"]], utcnow())
        rows = self.store.channels_for_refresh("YOUTUBE", "run-1")
        self.assertEqual([str(row["id"]) for row in rows], [str(second["id"]), str(first["id"])])

    def test_channel_hidden_subscribers_clear_unavailable_count_without_fabricated_zero(self):
        channel = self.channel("YOUTUBE")
        self.store.query("UPDATE channels SET subscriber_count = 12300 WHERE id = %s", (channel["id"],))
        metadata = youtube_channel(youtube_channel_item(statistics={"hiddenSubscriberCount": True, "viewCount": "0"}))
        self.store.save_channel_snapshot(channel["id"], metadata, utcnow(), "hidden")
        current = self.store.channel(channel["id"])
        history = self.store.query("SELECT * FROM channel_history")[0]
        self.assertIsNone(current["subscriber_count"])
        self.assertIsNone(history["subscriber_count"])
        self.assertEqual(history["total_views"], 0)

    def test_migration_can_be_applied_twice_without_losing_channels(self):
        channel = self.channel()
        self.migrate()
        self.assertTrue(self.store.channel(channel["id"])["is_tracked"])

    def test_receipt_and_mutations_roll_back_together(self):
        channel = self.channel()
        with self.assertRaises(RuntimeError), self.store.atomic():
            self.assertTrue(self.store.receipt("twitch", "event"))
            self.store.query("UPDATE channels SET is_tracked = FALSE WHERE id = %s", (channel["id"],))
            raise RuntimeError("database work failed")
        self.assertFalse(self.store.processed("twitch", "event"))
        self.assertTrue(self.store.channel(channel["id"])["is_tracked"])

    def test_samples_are_idempotent_and_aggregates_are_incremental(self):
        channel = self.channel()
        now = utcnow()
        video = {"id": "stream-1", "title": "Live", "status": "LIVE"}
        video_id = self.store.upsert_video(channel["id"], video, now)
        for offset, viewers in enumerate([0, 10, 30]):
            self.store.sample(video_id, viewers, now + timedelta(seconds=offset))
        self.store.sample(video_id, 100, now)
        row = self.store.query("SELECT * FROM videos WHERE id = %s", (video_id,))[0]
        self.assertEqual((row["ccv_sample_count"], row["ccv_sample_sum"], row["peak_ccv"], row["avg_ccv"]), (3, 40, 30, 13))

    def test_late_snapshot_does_not_overwrite_newer_video(self):
        channel = self.channel("YOUTUBE")
        now = utcnow()
        self.store.upsert_video(channel["id"], {"id": "one", "title": "New", "status": "ENDED"}, now)
        result = self.store.upsert_video(channel["id"], {"id": "one", "title": "Old", "status": "LIVE"}, now - timedelta(minutes=1))
        self.assertIsNone(result)
        self.assertEqual(self.store.query("SELECT title, stream_status FROM videos")[0], {"title": "New", "stream_status": "ENDED"})

    def test_late_offline_event_reconciles_current_broadcast_and_duplicates_skip_api(self):
        channel = self.channel()
        old = {"id": "old", "title": "Old", "status": "LIVE"}
        apply_snapshot(self.store, channel, utcnow() - timedelta(minutes=5), old, provider="twitch")
        platforms = MagicMock()
        platforms.twitch_streams.return_value = {"1234": {
            "id": "new", "title": "New", "started_at": utcnow().isoformat(), "viewer_count": 15,
        }}
        event = {"source": "streamer.twitch", "detail-type": "StreamLifecycleEvent", "detail": {
            "provider": "twitch", "kind": "stream.offline", "channel_id": str(channel["id"]), "event_key": "old-offline",
        }}
        self.assertEqual(handle(event, None, store_factory=self.open_store, platforms=platforms)["status"], "applied")
        self.assertEqual(handle(event, None, store_factory=self.open_store, platforms=platforms)["status"], "duplicate")
        self.assertEqual(platforms.twitch_streams.call_count, 1)
        rows = {row["platform_video_id"]: row["stream_status"] for row in self.store.query("SELECT * FROM videos")}
        self.assertEqual(rows, {"old": "ENDED", "new": "LIVE"})

    def test_websub_verification_can_arrive_before_registration_returns(self):
        self.channel("YOUTUBE")
        http = MagicMock()

        def request(method, url, **kwargs):
            if method == "POST":
                form = kwargs["form"]
                query = {key: value[0] for key, value in parse_qs(urlsplit(form["hub.callback"]).query).items()}
                query.update({"hub.mode": "subscribe", "hub.topic": form["hub.topic"], "hub.challenge": "challenge", "hub.lease_seconds": "7200"})
                event = api_event(method="GET")
                event["queryStringParameters"] = query
                result = youtube(event, None, store_factory=self.open_store, aws=self.aws)
                self.assertEqual((result["statusCode"], result["body"]), (200, "challenge"))
                return b""
            return feed().encode()

        http.request.side_effect = request
        with patch.dict(os.environ, {"YOUTUBE_CALLBACK_URL": "https://example.com/youtube/webhook"}):
            websub({}, None, store_factory=self.open_store, aws=self.aws, http=http)
            self.assertEqual(websub({}, None, store_factory=self.open_store, aws=self.aws, http=http)["attempted"], 0)
        sub = self.store.subscriptions("YOUTUBE")[0]
        self.assertEqual(sub["status"], "enabled")
        self.assertIsNone(sub["pending_action"])
        self.assertIsNotNone(sub["seeded_at"])

    def test_eventsub_verification_race_and_reconciliation(self):
        self.channel()
        platforms = MagicMock()
        remote = []
        platforms.twitch_subscriptions.side_effect = lambda: remote

        def create(method, path, **kwargs):
            payload = kwargs["payload"]
            sub_id = parse_qs(urlsplit(payload["transport"]["callback"]).query)["subscription_id"][0]
            sub = self.store.subscription(sub_id)
            sub["provider_subscription_id"] = "remote-" + payload["type"]
            event = twitch_event(sub, kind="webhook_callback_verification", challenge="challenge")
            result = twitch(event, None, store_factory=self.open_store, aws=self.aws)
            self.assertEqual((result["statusCode"], result["body"]), (200, "challenge"))
            item = {**payload, "id": sub["provider_subscription_id"], "status": "enabled"}
            remote.append(item)
            return {"data": [{**item, "status": "webhook_callback_verification_pending"}]}

        platforms.twitch.side_effect = create
        with patch.dict(os.environ, {"TWITCH_CALLBACK_URL": "https://example.com/twitch/webhook"}):
            self.assertEqual(eventsub({}, None, store_factory=self.open_store, aws=self.aws, platforms=platforms)["created"], 2)
            subs = self.store.subscriptions("TWITCH")
            self.assertTrue(all(sub["status"] == "verified" for sub in subs))
            self.assertTrue(all(sub["secret_version"] == 1 for sub in subs))
            self.assertEqual(eventsub({}, None, store_factory=self.open_store, aws=self.aws, platforms=platforms)["enabled"], 2)
        self.assertEqual(platforms.twitch.call_count, 2)

    def test_poller_preserves_missing_youtube_viewer_counts(self):
        channel = self.channel("YOUTUBE")
        self.store.upsert_video(channel["id"], {"id": "video", "title": "Upcoming", "status": "SCHEDULED"}, utcnow())
        platforms = MagicMock()
        platforms.youtube_videos.return_value = {"video": {
            "id": "video", "snippet": {"channelId": "UC-channel", "title": "Live", "liveBroadcastContent": "live"},
            "liveStreamingDetails": {"actualStartTime": utcnow().isoformat()},
        }}
        self.assertEqual(poll({}, None, store_factory=self.open_store, platforms=platforms)["sampled_channels"], 1)
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM ccv")[0]["n"], 0)
        self.assertEqual(self.store.query("SELECT stream_status FROM videos")[0]["stream_status"], "LIVE")

    def test_untracking_keeps_historical_data(self):
        channel = self.channel()
        self.store.upsert_video(channel["id"], {"id": "one", "title": "Old", "status": "ENDED"}, utcnow())
        self.store.ensure_subscription(channel, "stream.online", "https://example.com/twitch/webhook")
        self.store.query("UPDATE channels SET is_tracked = FALSE WHERE id = %s", (channel["id"],))
        self.assertFalse(self.store.subscriptions("TWITCH")[0]["desired"])
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM videos")[0]["n"], 1)
