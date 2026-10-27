import unittest
from unittest.mock import MagicMock

from tests.support import twitch_user_item, youtube_channel_item
from tracker.channels import optional_count, twitch_channel, update, youtube_channel


class ChannelNormalizationTests(unittest.TestCase):
    def test_youtube_profile_and_public_statistics(self):
        """Verify YouTube names, images, descriptions, and numeric statistics are normalized.

        Args:
            self (ChannelNormalizationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        metadata = youtube_channel(youtube_channel_item())
        self.assertEqual((metadata["channel_name"], metadata["subscriber_count"], metadata["total_views"], metadata["video_count"]),
                         ("Updated channel", 12300, 456789, 42))
        self.assertEqual(metadata["description"], "Channel description")
        self.assertEqual(metadata["profile_picture_url"], "https://images.example/youtube.png")

    def test_hidden_and_missing_counts_are_unknown_but_zero_is_valid(self):
        """Verify hidden/missing metrics remain unknown while reported zero remains a valid count.

        Args:
            self (ChannelNormalizationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        metadata = youtube_channel(youtube_channel_item(statistics={"hiddenSubscriberCount": True, "subscriberCount": "500", "viewCount": "0"}))
        self.assertIsNone(metadata["subscriber_count"])
        self.assertIsNone(metadata["video_count"])
        self.assertEqual(metadata["total_views"], 0)
        self.assertIsNone(youtube_channel(youtube_channel_item(statistics={}))['subscriber_count'])

    def test_invalid_counts_are_rejected(self):
        """Reject negative, fractional, boolean, and malformed channel counts.

        Args:
            self (ChannelNormalizationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        for value in [-1, "-1", True, 1.5, "unknown", "1.5"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                optional_count(value)

    def test_twitch_stores_followers_as_subscribers_without_deprecated_views(self):
        """Store the supplied follower count while ignoring paid totals and deprecated views.

        Args:
            self (ChannelNormalizationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        metadata = twitch_channel(twitch_user_item(view_count=9999, follower_count=123, subscriber_count=456))
        self.assertEqual(metadata["channel_name"], "Updated Twitch")
        self.assertEqual(metadata["profile_picture_url"], "https://images.example/twitch.png")
        self.assertEqual(metadata["subscriber_count"], 123)
        self.assertTrue(all(metadata[key] is None for key in ("total_views", "video_count")))

    def test_twitch_follower_counts_preserve_missing_and_zero(self):
        """Normalize supplied counts while preserving unknown followers and genuine zero.

        Args:
            self (ChannelNormalizationTests): Test-case instance providing assertions.

        Returns:
            None: Assertions complete without returning a value.
        """
        for value, expected in ((None, None), (0, 0), ("0", 0), ("123", 123)):
            with self.subTest(follower_count=value):
                metadata = twitch_channel(twitch_user_item(follower_count=value))
                self.assertEqual(metadata["subscriber_count"], expected)
        self.assertIsNone(twitch_channel(twitch_user_item())["subscriber_count"])
        for value in (-1, True, 1.5, "unknown"):
            with self.subTest(follower_count=value), self.assertRaises(ValueError):
                twitch_channel(twitch_user_item(follower_count=value))


class ChannelUpdateTests(unittest.TestCase):
    def setUp(self):
        """Prepare tracked YouTube/Twitch fixtures and injectable database/platform mocks.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Prepare fixture state on this test instance before each test.
        """
        self.youtube = {"id": "local-youtube", "platform_channel_id": "UC-channel"}
        self.twitch = {"id": "local-twitch", "platform_channel_id": "1234"}
        self.store = MagicMock()
        self.store.__enter__.return_value = self.store
        self.store.lock_job.return_value = True
        self.store.discover_channels.return_value = 0
        self.store.channels_for_refresh.side_effect = lambda provider, key: [self.youtube] if provider == "YOUTUBE" else [self.twitch]
        self.store.save_channel_snapshot.return_value = "updated"
        self.platforms = MagicMock()
        self.platforms.youtube_channels.return_value = {"UC-channel": youtube_channel_item()}
        self.platforms.twitch_users.return_value = {"1234": twitch_user_item(follower_count=123)}

    def invoke(self, event=None, context=None):
        """Run the channel updater with test adapters and an optional event or Lambda context.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.
            event (dict[str, Any] | None): Optional updater event; None selects an empty
                manual-invocation payload.
            context (LambdaContext | unittest.mock.MagicMock | None): Optional real/fake
                Lambda time-budget context for the test.

        Returns:
            dict[str, int | str]: Updater counters or an overlapping-job skipped reason from
                mocked adapters.
        """
        return update(event or {}, context, store_factory=lambda: self.store, platforms=self.platforms)

    def test_updates_both_platforms_with_stable_scheduled_key(self):
        """Verify both providers are refreshed using one stable scheduled collection key.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        result = self.invoke({"scheduled_at": "2026-10-26T12:00:00Z"})
        self.assertEqual(result["updated"], 2)
        self.platforms.youtube_channels.assert_called_once_with(["UC-channel"])
        self.platforms.twitch_users.assert_called_once_with(["1234"])
        keys = [call.args[3] for call in self.store.save_channel_snapshot.call_args_list]
        self.assertEqual(keys, ["scheduled:2026-10-26T12:00:00+00:00"] * 2)
        twitch_metadata = self.store.save_channel_snapshot.call_args_list[1].args[1]
        self.assertEqual(twitch_metadata["subscriber_count"], 123)

    def test_discovers_streamer_channels_before_selecting_refresh_candidates(self):
        """Verify streamer-linked channels are discovered before refresh candidate selection.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.store.discover_channels.return_value = 2
        result = self.invoke()
        self.assertEqual(result["discovered"], 2)
        calls = [call[0] for call in self.store.method_calls]
        self.assertLess(calls.index("discover_channels"), calls.index("channels_for_refresh"))

    def test_missing_channels_preserve_existing_data(self):
        """Verify an omitted channel creates no snapshot while the other provider still updates.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.platforms.youtube_channels.return_value = {}
        with self.assertLogs("tracker", level="WARNING"):
            result = self.invoke()
        self.assertEqual((result["missing"], result["updated"]), (1, 1))
        self.store.save_channel_snapshot.assert_called_once()

    def test_failure_in_one_platform_does_not_discard_other_platform(self):
        """Verify one provider failure is reported after preserving the other provider update.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.platforms.youtube_channels.side_effect = RuntimeError("temporary API failure")
        with self.assertLogs("tracker", level="ERROR"), self.assertRaises(RuntimeError):
            self.invoke()
        self.store.save_channel_snapshot.assert_called_once()
        self.assertEqual(self.store.save_channel_snapshot.call_args.args[0], "local-twitch")

    def test_incorrect_channel_identity_cannot_update_another_channel(self):
        """Reject mismatched returned identities while allowing unrelated channel updates.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.platforms.youtube_channels.return_value = {"UC-channel": youtube_channel_item("UC-other")}
        with self.assertLogs("tracker", level="ERROR"), self.assertRaises(RuntimeError):
            self.invoke()
        self.store.save_channel_snapshot.assert_called_once()
        self.assertEqual(self.store.save_channel_snapshot.call_args.args[0], "local-twitch")

    def test_near_deadline_defers_work_without_starting_network_requests(self):
        """Verify exhausted invocation time defers both providers without calling their APIs.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        context = MagicMock()
        context.get_remaining_time_in_millis.return_value = 100
        result = self.invoke(context=context)
        self.assertEqual(result["deferred"], 2)
        self.platforms.youtube_channels.assert_not_called()
        self.platforms.twitch_users.assert_not_called()

    def test_overlapping_job_is_skipped(self):
        """Verify an advisory-lock conflict prevents discovery and refresh work.

        Args:
            self (ChannelUpdateTests): Test-case instance containing fixtures, injected
                adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.store.lock_job.return_value = False
        self.assertEqual(self.invoke(), {"skipped": "already running"})
        self.store.channels_for_refresh.assert_not_called()
        self.store.discover_channels.assert_not_called()
