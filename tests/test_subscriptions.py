from datetime import timedelta
import unittest
from unittest.mock import MagicMock, patch

from tests.support import SECRET, aws_mock, feed, store_for, subscription
from tracker.configuration import CONFIG
from tracker.platforms import callback_url
from tracker.runtime import ApiError, utcnow
from tracker.subscriptions import eventsub, websub


class WebSubManagerTests(unittest.TestCase):
    def setUp(self):
        """Prepare a healthy YouTube subscription and mocked hub, AWS, and store adapters.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Prepare fixture state on this test instance before each test.
        """
        self.sub = subscription()
        self.store = store_for(self.sub)
        self.aws = aws_mock()
        self.http = MagicMock()
        self.http.request.return_value = feed().encode()

    def invoke(self):
        """Invoke WebSub reconciliation with the fixture callback URL and mocked adapters.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            dict[str, int | str]: WebSub manager counters or a skipped result produced using
                fixture adapters.
        """
        with patch.dict("os.environ", {"YOUTUBE_CALLBACK_URL": self.sub["callback_url"]}):
            return websub({}, None, store_factory=lambda: self.store, aws=self.aws, http=self.http)

    def test_healthy_subscription_is_not_renewed_on_every_run(self):
        """Verify a healthy lease does not issue a renewal request on every maintenance run.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke()["attempted"], 0)
        self.http.request.assert_not_called()

    def test_five_day_age_forces_refresh_despite_later_stored_renewal(self):
        """Verify the five-day age cap overrides a later renewal date without resetting verification.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        now = utcnow()
        verified = now - timedelta(days=5)
        self.sub.update(verified_at=verified, renew_at=now + timedelta(days=10), lease_expires_at=now + timedelta(days=20))
        with patch("tracker.subscriptions.utcnow", return_value=now):
            self.assertEqual(self.invoke()["attempted"], 1)
        self.assertEqual(self.http.request.call_args.kwargs["form"]["hub.mode"], "subscribe")
        # Accepting a request does not reset the age cap; verification does.
        self.assertEqual(self.sub["verified_at"], verified)

    def test_long_lease_is_not_refreshed_before_age_cap_when_not_otherwise_due(self):
        """Verify a healthy long lease stays untouched immediately before its mandatory age cap.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        now = utcnow()
        self.sub.update(verified_at=now - timedelta(days=5) + timedelta(seconds=1),
                        renew_at=now + timedelta(days=10), lease_expires_at=now + timedelta(days=20))
        with patch("tracker.subscriptions.utcnow", return_value=now):
            self.assertEqual(self.invoke()["attempted"], 0)
        self.http.request.assert_not_called()

    def test_missing_verification_time_requires_refresh(self):
        """Verify an enabled-looking subscription without verification time is renewed.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub.update(verified_at=None)
        self.assertEqual(self.invoke()["attempted"], 1)

    def test_refresh_age_cap_uses_runtime_configuration(self):
        """Verify mandatory renewal age follows its YAML setting rather than a hardcoded interval.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub.update(verified_at=utcnow() - timedelta(days=3))
        with patch.dict(CONFIG["youtube"], {"max_refresh_interval_seconds": 172800}):
            self.assertEqual(self.invoke()["attempted"], 1)

    def test_due_subscription_is_renewed_with_stable_secret(self):
        """Verify a due renewal reuses the configured stable signing secret.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub["renew_at"] = utcnow() - timedelta(seconds=1)
        self.assertEqual(self.invoke()["attempted"], 1)
        form = self.http.request.call_args.kwargs["form"]
        self.assertEqual(form["hub.secret"], SECRET)
        self.assertEqual(form["hub.mode"], "subscribe")

    def test_request_acceptance_does_not_mark_subscription_verified(self):
        """Verify hub acceptance keeps registration pending while allowing initial feed seeding.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub.update(status="missing", verified_at=None, seeded_at=None, lease_expires_at=None, renew_at=None)
        self.invoke()
        self.assertEqual(self.sub["status"], "pending")
        self.assertIsNone(self.sub["verified_at"])
        self.aws.publish.assert_called_once()

    def test_hub_error_persists_backoff(self):
        """Verify a rate-limited hub attempt persists its retry delay and reports job failure.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub["renew_at"] = utcnow() - timedelta(seconds=1)
        self.http.request.side_effect = ApiError(429, 900)
        with self.assertLogs("tracker", level="ERROR"), self.assertRaises(RuntimeError):
            self.invoke()
        self.assertGreater(self.sub["retry_at"], utcnow() + timedelta(minutes=14))

    def test_removed_channel_requests_unsubscribe(self):
        """Verify an undesired YouTube subscription requests removal from the hub.

        Args:
            self (WebSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub["desired"] = False
        self.invoke()
        self.assertEqual(self.http.request.call_args.kwargs["form"]["hub.mode"], "unsubscribe")


class EventSubManagerTests(unittest.TestCase):
    def setUp(self):
        """Prepare a healthy Twitch registration and mocked store, AWS, and API adapters.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Prepare fixture state on this test instance before each test.
        """
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
        """Invoke EventSub reconciliation with the fixture callback and mocked adapters.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            dict[str, int | str]: EventSub reconciliation counters or a skipped result from
                fixture adapters.
        """
        with patch.dict("os.environ", {"TWITCH_CALLBACK_URL": self.sub["callback_url"]}):
            return eventsub({}, None, store_factory=lambda: self.store, aws=self.aws, platforms=self.platforms)

    def test_enabled_remote_is_not_recreated_daily(self):
        """Verify an enabled matching Twitch registration is retained without create/delete calls.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke()["enabled"], 1)
        self.platforms.twitch.assert_not_called()

    def test_missing_subscription_is_created(self):
        """Verify a missing Twitch registration is created with the signing secret and recorded.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.platforms.twitch_subscriptions.return_value = []
        self.platforms.twitch.return_value = {"data": [{"id": "new-id", "status": "webhook_callback_verification_pending"}]}
        self.assertEqual(self.invoke()["created"], 1)
        self.assertEqual(self.platforms.twitch.call_args.kwargs["payload"]["transport"]["secret"], SECRET)
        self.store.record_twitch_created.assert_called_once()

    def test_untracked_subscription_is_deleted(self):
        """Verify an undesired owned Twitch registration is deleted and marked unsubscribed.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub["desired"] = False
        self.invoke()
        self.assertEqual(self.platforms.twitch.call_args.args[:2], ("DELETE", "eventsub/subscriptions"))
        self.assertEqual(self.sub["status"], "unsubscribed")

    def test_other_environments_subscription_is_not_deleted(self):
        """Verify another environment callback prevents deletion of its Twitch registration.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub["desired"] = False
        self.remote["transport"]["callback"] = "https://other-environment.example/webhook"
        self.invoke()
        self.platforms.twitch.assert_not_called()

    def test_concurrent_create_conflict_refreshes_remote_list(self):
        """Verify a Twitch creation conflict refreshes the remote subscription listing.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.platforms.twitch_subscriptions.return_value = []
        self.platforms.twitch.side_effect = ApiError(409)
        self.invoke()
        self.assertEqual(self.platforms.twitch_subscriptions.call_count, 2)

    def test_permanent_revocation_does_not_loop_recreating(self):
        """Verify permanent authorization revocation does not trigger a recreation loop.

        Args:
            self (EventSubManagerTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.remote["status"] = "authorization_revoked"
        self.invoke()
        self.platforms.twitch.assert_not_called()
