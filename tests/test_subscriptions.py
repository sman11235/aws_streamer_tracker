from datetime import timedelta
import unittest
from unittest.mock import MagicMock, patch

from tests.support import SECRET, aws_mock, feed, store_for, subscription
from tracker.platforms import callback_url
from tracker.runtime import ApiError, utcnow
from tracker.subscriptions import eventsub, websub


class WebSubManagerTests(unittest.TestCase):
    def setUp(self):
        self.sub = subscription()
        self.store = store_for(self.sub)
        self.aws = aws_mock()
        self.http = MagicMock()
        self.http.request.return_value = feed().encode()

    def invoke(self):
        with patch.dict("os.environ", {"YOUTUBE_CALLBACK_URL": self.sub["callback_url"]}):
            return websub({}, None, store_factory=lambda: self.store, aws=self.aws, http=self.http)

    def test_healthy_subscription_is_not_renewed_on_every_run(self):
        self.assertEqual(self.invoke()["attempted"], 0)
        self.http.request.assert_not_called()

    def test_due_subscription_is_renewed_with_stable_secret(self):
        self.sub["renew_at"] = utcnow() - timedelta(seconds=1)
        self.assertEqual(self.invoke()["attempted"], 1)
        form = self.http.request.call_args.kwargs["form"]
        self.assertEqual(form["hub.secret"], SECRET)
        self.assertEqual(form["hub.mode"], "subscribe")

    def test_request_acceptance_does_not_mark_subscription_verified(self):
        self.sub.update(status="missing", verified_at=None, seeded_at=None, lease_expires_at=None, renew_at=None)
        self.invoke()
        self.assertEqual(self.sub["status"], "pending")
        self.assertIsNone(self.sub["verified_at"])
        self.aws.publish.assert_called_once()

    def test_hub_error_persists_backoff(self):
        self.sub["renew_at"] = utcnow() - timedelta(seconds=1)
        self.http.request.side_effect = ApiError(429, 900)
        with self.assertLogs("tracker", level="ERROR"), self.assertRaises(RuntimeError):
            self.invoke()
        self.assertGreater(self.sub["retry_at"], utcnow() + timedelta(minutes=14))

    def test_removed_channel_requests_unsubscribe(self):
        self.sub["desired"] = False
        self.invoke()
        self.assertEqual(self.http.request.call_args.kwargs["form"]["hub.mode"], "unsubscribe")


class EventSubManagerTests(unittest.TestCase):
    def setUp(self):
        self.sub = subscription("TWITCH")
        self.store = store_for(self.sub)
        self.aws = aws_mock()
        self.platforms = MagicMock()
        self.remote = {
            "id": "remote-1", "type": "stream.online", "version": "1", "status": "enabled",
            "condition": {"broadcaster_user_id": self.sub["platform_channel_id"]},
            "transport": {"method": "webhook", "callback": callback_url(self.sub)},
        }
        self.platforms.twitch_subscriptions.return_value = [self.remote]

    def invoke(self):
        with patch.dict("os.environ", {"TWITCH_CALLBACK_URL": self.sub["callback_url"]}):
            return eventsub({}, None, store_factory=lambda: self.store, aws=self.aws, platforms=self.platforms)

    def test_enabled_remote_is_not_recreated_daily(self):
        self.assertEqual(self.invoke()["enabled"], 1)
        self.platforms.twitch.assert_not_called()

    def test_missing_subscription_is_created(self):
        self.platforms.twitch_subscriptions.return_value = []
        self.platforms.twitch.return_value = {"data": [{"id": "new-id", "status": "webhook_callback_verification_pending"}]}
        self.assertEqual(self.invoke()["created"], 1)
        self.assertEqual(self.platforms.twitch.call_args.kwargs["payload"]["transport"]["secret"], SECRET)
        self.store.record_twitch_created.assert_called_once()

    def test_untracked_subscription_is_deleted(self):
        self.sub["desired"] = False
        self.invoke()
        self.assertEqual(self.platforms.twitch.call_args.args[:2], ("DELETE", "eventsub/subscriptions"))
        self.assertEqual(self.sub["status"], "unsubscribed")

    def test_other_environments_subscription_is_not_deleted(self):
        self.sub["desired"] = False
        self.remote["transport"]["callback"] = "https://other-environment.example/webhook"
        self.invoke()
        self.platforms.twitch.assert_not_called()

    def test_concurrent_create_conflict_refreshes_remote_list(self):
        self.platforms.twitch_subscriptions.return_value = []
        self.platforms.twitch.side_effect = ApiError(409)
        self.invoke()
        self.assertEqual(self.platforms.twitch_subscriptions.call_count, 2)

    def test_permanent_revocation_does_not_loop_recreating(self):
        self.remote["status"] = "authorization_revoked"
        self.invoke()
        self.platforms.twitch.assert_not_called()
