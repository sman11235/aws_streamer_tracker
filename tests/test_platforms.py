import time
import unittest
from unittest.mock import MagicMock

from tests.support import aws_mock
from tracker.platforms import Platforms
from tracker.runtime import ApiError
from tracker.streams import youtube_video


class PlatformTests(unittest.TestCase):
    def test_youtube_channel_request_batches_ids_and_selects_statistics(self):
        """Verify a channel request batches IDs and requests profile/statistics resource parts.

        Args:
            self (PlatformTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        http = MagicMock()
        http.json.return_value = {"items": [{"id": "UC-one"}, {"id": "UC-two"}]}
        platforms = Platforms(aws=aws_mock(), http=http)
        self.assertEqual(set(platforms.youtube_channels(["UC-one", "UC-two"])), {"UC-one", "UC-two"})
        params = http.json.call_args.kwargs["params"]
        self.assertEqual((params["id"], params["maxResults"], params["part"]), ("UC-one,UC-two", 2, "snippet,statistics"))

    def test_twitch_user_request_uses_ids_instead_of_logins(self):
        """Verify Twitch user lookups send broadcaster IDs and key results by ID.

        Args:
            self (PlatformTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        platforms = Platforms()
        platforms.twitch = MagicMock(return_value={"data": [{"id": "1"}, {"id": "2"}]})
        self.assertEqual(set(platforms.twitch_users(["1", "2"])), {"1", "2"})
        self.assertEqual(platforms.twitch.call_args.kwargs["params"], {"id": ["1", "2"]})

    def test_expired_twitch_authorization_is_retried_with_new_app_token(self):
        """Verify a Helix 401 obtains a fresh app token and retries with that token.

        Args:
            self (PlatformTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        http = MagicMock()
        http.json.side_effect = [
            {"access_token": "first", "expires_in": 3600}, ApiError(401),
            {"access_token": "second", "expires_in": 3600}, {"data": []},
        ]
        platforms = Platforms(aws=aws_mock(), http=http)
        self.assertEqual(platforms.twitch_streams(["1234"]), {})
        self.assertEqual(http.json.call_args.kwargs["headers"]["Authorization"], "Bearer second")

    def test_long_lived_twitch_session_is_validated_hourly(self):
        """Verify an old cached token is validated before another Helix request.

        Args:
            self (PlatformTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        http = MagicMock()
        http.json.side_effect = [{"client_id": "client"}, {"data": []}]
        platforms = Platforms(aws=aws_mock(), http=http)
        platforms.token = "cached"
        platforms.token_expiry = time.monotonic() + 10000
        platforms.token_validated_at = time.monotonic() - 3601
        platforms.twitch_streams(["1234"])
        self.assertEqual(http.json.call_args_list[0].args[1], "https://id.twitch.tv/oauth2/validate")

    def test_subscription_listing_follows_all_pages(self):
        """Verify EventSub listing follows pagination cursors and combines every page.

        Args:
            self (PlatformTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        platforms = Platforms()
        platforms.twitch = MagicMock(side_effect=[
            {"data": [{"id": "a"}], "pagination": {"cursor": "next"}},
            {"data": [{"id": "b"}], "pagination": {}},
        ])
        self.assertEqual([item["id"] for item in platforms.twitch_subscriptions()], ["a", "b"])
        self.assertEqual(platforms.twitch.call_args.kwargs["params"]["after"], "next")

    def test_ordinary_upload_is_not_treated_as_live(self):
        """Verify ordinary YouTube uploads produce no live-stream snapshot.

        Args:
            self (PlatformTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        self.assertIsNone(youtube_video({"id": "upload", "snippet": {"title": "A regular upload"}}))

    def test_ended_video_wins_over_stale_live_flag(self):
        """Verify an actual YouTube end time overrides a stale live flag and removes live viewers.

        Args:
            self (PlatformTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        video = youtube_video({
            "id": "ended", "snippet": {"title": "Stream", "liveBroadcastContent": "live"},
            "liveStreamingDetails": {"actualEndTime": "2026-10-26T10:00:00Z"},
        })
        self.assertEqual(video["status"], "ENDED")
        self.assertIsNone(video["viewers"])
