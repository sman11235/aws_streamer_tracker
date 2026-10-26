"""Public platform APIs and YouTube feed parsing."""

import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from defusedxml.ElementTree import fromstring

from tracker.configuration import CONFIG
from tracker.runtime import AWS_CLIENT, HTTP_CLIENT, ApiError, fingerprint


# XML namespace identifiers are protocol constants, not service endpoints.
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def callback_url(subscription):
    """Return the subscription callback URL with its local subscription UUID appended.

    Args:
        subscription (Mapping[str, Any]): Subscription row containing callback_url and
            its local id.

    Returns:
        str: Callback URL with the subscription_id query parameter appended.
    """
    parts = urlsplit(subscription["callback_url"])
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("subscription_id", str(subscription["id"])))
    return urlunsplit(parts._replace(query=urlencode(query)))


def youtube_topic(channel_id):
    """Build the configured public feed URL for a YouTube channel ID.

    Args:
        channel_id (str): Canonical YouTube channel ID to encode into the public feed
            URL.

    Returns:
        str: Configured public feed endpoint with its channel_id query parameter.
    """
    return CONFIG["youtube"]["feed_url"] + "?" + urlencode({"channel_id": channel_id})


def youtube_entries(body):
    """Safely parse an Atom feed into normalized YouTube video-change entries.

    Args:
        body (bytes | str): Atom XML from a public feed response or verified webhook
            body.

    Returns:
        list[dict[str, str]]: Entries containing video_id, platform_channel_id, updated
            timestamp, and event_key.

    Raises:
        ValueError: The root is not an Atom feed or required fields are missing.
        ParseError: The XML is malformed.
        DefusedXmlException: The XML contains unsafe constructs.
    """
    root = fromstring(body)
    if root.tag != ATOM + "feed":
        raise ValueError("Expected an Atom feed")
    entries = []
    for entry in root.findall(ATOM + "entry"):
        video_id = entry.findtext(YT + "videoId")
        channel_id = entry.findtext(YT + "channelId")
        updated = entry.findtext(ATOM + "updated") or entry.findtext(ATOM + "published")
        if not video_id or not channel_id or not updated:
            raise ValueError("Incomplete YouTube feed entry")
        entries.append({
            "video_id": video_id, "platform_channel_id": channel_id, "updated": updated,
            "event_key": fingerprint(channel_id, video_id, updated, entry.findtext(ATOM + "title")),
        })
    return entries


class Platforms:
    def __init__(self, aws=AWS_CLIENT, http=HTTP_CLIENT):
        """Store AWS/HTTP adapters and initialize the cached Twitch app token state.

        Args:
            self (Platforms): Platform client instance holding its adapters and cached
                Twitch token.
            aws (AWS): Injectable AWS adapter resolving API keys and Twitch client
                credentials from SSM.
            http (HTTP): Injectable HTTP adapter used for OAuth and platform API requests.

        Returns:
            None: Store adapters and initialize empty cached Twitch authorization state.
        """
        self.aws = aws
        self.http = http
        self.token = None
        self.token_expiry = 0
        self.token_validated_at = 0

    def twitch(self, method, path, **kwargs):
        """Call a configured Twitch Helix path with app authentication.

        Cache app tokens, validate them at the configured interval, and obtain a
        fresh token after unauthorized responses within the configured attempt limit.
        Other API and decoding errors propagate to the caller.

        Args:
            self (Platforms): Platform client instance holding its adapters and cached
                Twitch token.
            method (str): HTTP method used for the Helix API operation.
            path (str): API resource path relative to the configured Helix base URL.
            **kwargs (dict[str, Any]): HTTP.json options such as params, form, or payload.

        Returns:
            dict[str, Any] | list[Any] | str | int | float | bool | None: Decoded JSON
                response from the Helix API; its shape depends on the resource.
        """
        settings = CONFIG["twitch"]
        for attempt in range(settings["authorization_attempts"]):
            if self.token and time.monotonic() - self.token_validated_at >= settings["token_validation_interval_seconds"]:
                try:
                    self.http.json("GET", settings["token_validation_url"], headers={"Authorization": "OAuth " + self.token})
                    self.token_validated_at = time.monotonic()
                except ApiError as error:
                    if error.status != 401:
                        raise
                    self.token = None
            if self.token is None or self.token_expiry < time.monotonic():
                client_id, _ = self.aws.setting("twitch_client_id")
                client_secret, _ = self.aws.setting("twitch_client_secret")
                result = self.http.json("POST", settings["token_url"], form={
                    "client_id": client_id, "client_secret": client_secret,
                    "grant_type": "client_credentials",
                })
                self.token = result["access_token"]
                self.token_expiry = time.monotonic() + max(0, int(result["expires_in"]) - settings["token_expiry_margin_seconds"])
                self.token_validated_at = time.monotonic()
            client_id, _ = self.aws.setting("twitch_client_id")
            try:
                return self.http.json(method, settings["helix_url"] + path, headers={
                    "Client-Id": client_id, "Authorization": "Bearer " + self.token,
                }, **kwargs)
            except ApiError as error:
                if error.status != 401 or attempt == settings["authorization_attempts"] - 1:
                    raise
                self.token = None

    def twitch_subscriptions(self):
        """Return all EventSub subscriptions across every pagination page.

        Raise RuntimeError if Twitch repeats a pagination cursor, preventing an
        endless listing loop. Use the configured page size for each request.

        Args:
            self (Platforms): Platform client instance holding its adapters and cached
                Twitch token.

        Returns:
            list[dict[str, Any]]: Remote EventSub subscription objects combined from all
                pages.
        """
        result = []
        cursor = None
        seen = set()
        while True:
            params = {"first": CONFIG["twitch"]["subscription_page_size"]}
            if cursor:
                params["after"] = cursor
            page = self.twitch("GET", CONFIG["twitch"]["subscriptions_path"], params=params)
            result.extend(page["data"])
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                return result
            if cursor in seen:
                raise RuntimeError("Twitch repeated its pagination cursor")
            seen.add(cursor)

    def twitch_streams(self, user_ids):
        """Fetch one batch of current broadcasts, keyed by broadcaster user ID.

        Return an empty dictionary for no IDs or no live results. Callers must split
        larger channel lists into batches within Twitch's configured API limit.

        Args:
            self (Platforms): Platform client instance holding its adapters and cached
                Twitch token.
            user_ids (Sequence[str]): At most one configured batch of Twitch broadcaster
                user IDs.

        Returns:
            dict[str, dict[str, Any]]: Current live stream objects keyed by broadcaster user
                ID; offline IDs are absent.
        """
        if not user_ids:
            return {}
        page = self.twitch("GET", CONFIG["twitch"]["streams_path"], params={"user_id": user_ids, "first": CONFIG["twitch"]["polling_batch_size"]})
        return {stream["user_id"]: stream for stream in page["data"]}

    def youtube_videos(self, video_ids):
        """Fetch one batch of YouTube video metadata keyed by video ID.

        Use the API key from SSM and the configured endpoint and resource parts.
        Missing video IDs are absent from the returned dictionary.

        Args:
            self (Platforms): Platform client instance holding its adapters and cached
                Twitch token.
            video_ids (Sequence[str]): At most one configured batch of YouTube video IDs to
                look up.

        Returns:
            dict[str, dict[str, Any]]: Video resource objects keyed by video ID; unavailable
                IDs are absent.
        """
        key, _ = self.aws.setting("youtube_api_key")
        result = self.http.json("GET", CONFIG["youtube"]["videos_url"], params={
            "part": ",".join(CONFIG["youtube"]["video_parts"]), "id": ",".join(video_ids), "key": key,
        })
        return {video["id"]: video for video in result["items"]}

    def youtube_channels(self, channel_ids):
        """Fetch public channel profiles/statistics keyed by YouTube channel ID.

        Return an empty dictionary for an empty input. Callers batch IDs to respect
        the API limit; omitted or unavailable channels are absent from the result.

        Args:
            self (Platforms): Platform client instance holding its adapters and cached
                Twitch token.
            channel_ids (Sequence[str]): At most one configured batch of YouTube channel
                IDs.

        Returns:
            dict[str, dict[str, Any]]: Channel profile/statistics resources keyed by
                canonical channel ID.
        """
        if not channel_ids:
            return {}
        key, _ = self.aws.setting("youtube_api_key")
        result = self.http.json("GET", CONFIG["youtube"]["channels_url"], params={
            "part": ",".join(CONFIG["youtube"]["channel_parts"]),
            "id": ",".join(channel_ids), "maxResults": len(channel_ids), "key": key,
        })
        return {channel["id"]: channel for channel in result["items"]}

    def twitch_users(self, user_ids):
        """Fetch one batch of public Twitch user profiles keyed by user ID.

        Args:
            self (Platforms): Platform client instance holding its adapters and cached
                Twitch token.
            user_ids (Sequence[str]): At most one configured batch of Twitch user IDs, not
                login names.

        Returns:
            dict[str, dict[str, Any]]: Public Twitch user objects keyed by user ID.
        """
        if not user_ids:
            return {}
        result = self.twitch("GET", CONFIG["twitch"]["users_path"], params={"id": user_ids})
        return {user["id"]: user for user in result["data"]}


PLATFORMS = Platforms()
