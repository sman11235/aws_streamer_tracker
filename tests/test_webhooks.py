from datetime import timedelta
import unittest

from tests.support import api_event, aws_mock, feed, store_for, subscription, twitch_event, youtube_event
from tracker.runtime import AWS, utcnow
from tracker.webhooks import twitch, youtube


class YouTubeWebhookTests(unittest.TestCase):
    def setUp(self):
        """Prepare a YouTube subscription with mocked persistence and AWS delivery.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Prepare fixture state on this test instance before each test.
        """
        self.sub = subscription()
        self.store = store_for(self.sub)
        self.aws = aws_mock()

    def invoke(self, event):
        """Invoke the YouTube webhook with the prepared store and AWS adapters.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.
            event (dict[str, Any]): API Gateway YouTube webhook request fixture to process.

        Returns:
            dict[str, Any]: API Gateway response returned by the YouTube handler.
        """
        return youtube(event, None, store_factory=lambda: self.store, aws=self.aws)

    def challenge(self, **query):
        """Prepare pending WebSub state and a matching GET challenge with optional query overrides.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.
            **query (dict[str, str]): Hub verification query fields overriding the matching
                default challenge.

        Returns:
            dict[str, Any]: GET verification event paired with freshly prepared pending
                subscription state.
        """
        self.sub.update(pending_action="subscribe", pending_since=utcnow(), pending_secret_version=1)
        event = api_event(method="GET", sub=self.sub)
        event["queryStringParameters"].update({
            "hub.mode": "subscribe", "hub.topic": self.sub["topic_url"],
            "hub.challenge": "challenge-exact", "hub.lease_seconds": "7200", **query,
        })
        return event

    def test_verification_records_actual_lease_and_returns_raw_challenge(self):
        """Verify the exact challenge, actual granted lease, and early renewal deadline are recorded.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        before = utcnow()
        result = self.invoke(self.challenge())
        self.assertEqual(result["body"], "challenge-exact")
        self.assertEqual(result["statusCode"], 200)
        self.assertAlmostEqual((self.sub["lease_expires_at"] - before).total_seconds(), 7200, delta=1)
        self.assertEqual(self.sub["status"], "enabled")
        self.assertEqual((self.sub["renew_at"] - self.sub["verified_at"]).total_seconds(), 5760)

    def test_long_granted_lease_has_five_day_mandatory_renewal_deadline(self):
        """Verify a long granted lease remains intact while mandatory renewal is capped at five days.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        result = self.invoke(self.challenge(**{"hub.lease_seconds": "2592000"}))
        self.assertEqual(result["statusCode"], 200)
        verified = self.sub["verified_at"]
        self.assertEqual((self.sub["lease_expires_at"] - verified).total_seconds(), 2592000)
        self.assertEqual((self.sub["renew_at"] - verified).total_seconds(), 432000)

    def test_unknown_topic_is_rejected(self):
        """Verify a challenge for another topic cannot modify the subscription.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke(self.challenge(**{"hub.topic": "https://attacker.example/"}))["statusCode"], 404)
        self.store.update_subscription.assert_not_called()

    def test_zero_lease_is_rejected(self):
        """Verify a zero-duration granted WebSub lease is rejected.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke(self.challenge(**{"hub.lease_seconds": "0"}))["statusCode"], 400)

    def test_unsolicited_verification_is_rejected(self):
        """Verify a challenge without a pending subscription action is rejected.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        event = self.challenge()
        self.sub["pending_action"] = None
        self.assertEqual(self.invoke(event)["statusCode"], 404)

    def test_valid_base64_delivery_publishes_video_change(self):
        """Verify a signed base64 YouTube body publishes a normalized video-change event.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke(youtube_event(self.sub, encoded=True))["statusCode"], 204)
        self.assertEqual(self.aws.publish.call_args.args[1][0]["kind"], "video.changed")

    def test_bad_signature_is_acknowledged_but_discarded(self):
        """Verify tampered YouTube notifications are acknowledged but never published.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        event = youtube_event(self.sub)
        event["body"] += "tampered"
        self.assertEqual(self.invoke(event)["statusCode"], 204)
        self.aws.publish.assert_not_called()

    def test_other_channel_cannot_be_injected(self):
        """Verify even a signed feed cannot inject a different channel into this subscription.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke(youtube_event(self.sub, feed(channel="another")))["statusCode"], 400)
        self.aws.publish.assert_not_called()

    def test_xml_entities_are_rejected(self):
        """Verify external-entity XML is rejected before any event is published.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        body = '<!DOCTYPE feed [<!ENTITY x SYSTEM "file:///etc/passwd">]><feed>&x;</feed>'
        self.assertEqual(self.invoke(youtube_event(self.sub, body))["statusCode"], 400)
        self.aws.publish.assert_not_called()

    def test_eventbridge_failure_returns_retryable_status(self):
        """Verify YouTube delivery returns HTTP 503 when EventBridge publication fails.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.aws.publish.side_effect = RuntimeError("failed")
        with self.assertLogs("tracker", level="ERROR"):
            self.assertEqual(self.invoke(youtube_event(self.sub))["statusCode"], 503)

    def test_untracked_channel_does_not_publish(self):
        """Verify an untracked YouTube channel notification is discarded without publication.

        Args:
            self (YouTubeWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub["is_tracked"] = False
        self.assertEqual(self.invoke(youtube_event(self.sub))["statusCode"], 204)
        self.aws.publish.assert_not_called()


class TwitchWebhookTests(unittest.TestCase):
    def setUp(self):
        """Prepare a Twitch subscription with mocked persistence and AWS delivery.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Prepare fixture state on this test instance before each test.
        """
        self.sub = subscription("TWITCH")
        self.store = store_for(self.sub)
        self.aws = aws_mock()

    def invoke(self, event):
        """Invoke the Twitch webhook with the prepared store and AWS adapters.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.
            event (dict[str, Any]): API Gateway Twitch webhook request fixture to process.

        Returns:
            dict[str, Any]: API Gateway response returned by the Twitch handler.
        """
        return twitch(event, None, store_factory=lambda: self.store, aws=self.aws)

    def test_signed_challenge_returns_plain_text(self):
        """Verify a correctly signed pending Twitch challenge is returned exactly and recorded.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.sub.update(pending_action="subscribe", pending_secret_version=1, pending_since=utcnow())
        result = self.invoke(twitch_event(self.sub, kind="webhook_callback_verification", challenge="exact-challenge"))
        self.assertEqual((result["statusCode"], result["body"]), (200, "exact-challenge"))
        self.assertEqual(self.sub["status"], "verified")

    def test_base64_body_and_original_bytes_are_verified(self):
        """Verify base64 decoding restores the original bytes used by Twitch signature verification.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke(twitch_event(self.sub, encoded=True))["statusCode"], 204)
        self.aws.publish.assert_called_once()

    def test_bad_signature_cannot_publish(self):
        """Verify a tampered Twitch body is forbidden and never published.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        event = twitch_event(self.sub)
        event["body"] += " "
        self.assertEqual(self.invoke(event)["statusCode"], 403)
        self.aws.publish.assert_not_called()

    def test_stale_message_is_rejected(self):
        """Verify a Twitch message older than the freshness window is forbidden.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        sent_at = (utcnow() - timedelta(minutes=11)).isoformat()
        self.assertEqual(self.invoke(twitch_event(self.sub, sent_at=sent_at))["statusCode"], 403)

    def test_wrong_subscription_id_is_rejected(self):
        """Verify a mismatched remote Twitch subscription ID is forbidden.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        event = twitch_event(self.sub)
        self.sub["provider_subscription_id"] = "different"
        self.assertEqual(self.invoke(event)["statusCode"], 403)

    def test_delivery_failure_is_retryable(self):
        """Verify Twitch delivery returns HTTP 503 when event publication fails.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.aws.publish.side_effect = RuntimeError("failed")
        with self.assertLogs("tracker", level="ERROR"):
            self.assertEqual(self.invoke(twitch_event(self.sub))["statusCode"], 503)

    def test_another_broadcaster_is_rejected(self):
        """Verify a signed Twitch notification naming another broadcaster is forbidden.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        result = self.invoke(twitch_event(self.sub, event={"broadcaster_user_id": "someone-else"}))
        self.assertEqual(result["statusCode"], 403)

    def test_duplicate_revocation_is_only_applied_once(self):
        """Verify repeated Twitch revocations update local subscription state only once.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.store.receipt.side_effect = [True, False]
        event = twitch_event(self.sub, kind="revocation")
        self.assertEqual(self.invoke(event)["statusCode"], 204)
        self.assertEqual(self.invoke(event)["statusCode"], 204)
        self.store.update_subscription.assert_called_once()

    def test_missing_headers_are_bad_request(self):
        """Verify Twitch messages without required delivery headers receive HTTP 400.

        Args:
            self (TwitchWebhookTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertEqual(self.invoke(api_event("{}", sub=self.sub))["statusCode"], 400)


class AWSDeliveryTests(unittest.TestCase):
    def test_partial_eventbridge_failure_is_not_success(self):
        """Verify an individual EventBridge entry failure raises despite a successful API response.

        Args:
            self (AWSDeliveryTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        from unittest.mock import MagicMock, patch
        aws = AWS()
        client = MagicMock()
        client.put_events.return_value = {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]}
        aws.clients["events"] = client
        with patch.dict("os.environ", {"EVENT_BUS_NAME": "test"}):
            with self.assertRaises(RuntimeError):
                aws.publish("youtube", [{"video_id": "one"}])

    def test_large_feed_is_split_into_eventbridge_batches(self):
        """Verify 23 events are published in batches of ten, ten, and three.

        Args:
            self (AWSDeliveryTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        from unittest.mock import MagicMock, patch
        aws = AWS()
        client = MagicMock()
        client.put_events.return_value = {"FailedEntryCount": 0}
        aws.clients["events"] = client
        with patch.dict("os.environ", {"EVENT_BUS_NAME": "test"}):
            aws.publish("youtube", [{"video_id": str(i)} for i in range(23)])
        self.assertEqual([len(call.kwargs["Entries"]) for call in client.put_events.call_args_list], [10, 10, 3])
