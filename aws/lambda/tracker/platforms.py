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
    parts = urlsplit(subscription["callback_url"])
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("subscription_id", str(subscription["id"])))
    return urlunsplit(parts._replace(query=urlencode(query)))


def youtube_topic(channel_id):
    return CONFIG["youtube"]["feed_url"] + "?" + urlencode({"channel_id": channel_id})


def youtube_entries(body):
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
        self.aws = aws
        self.http = http
        self.token = None
        self.token_expiry = 0
        self.token_validated_at = 0

    def twitch(self, method, path, **kwargs):
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
        if not user_ids:
            return {}
        page = self.twitch("GET", CONFIG["twitch"]["streams_path"], params={"user_id": user_ids, "first": CONFIG["twitch"]["polling_batch_size"]})
        return {stream["user_id"]: stream for stream in page["data"]}

    def youtube_videos(self, video_ids):
        key, _ = self.aws.setting("youtube_api_key")
        result = self.http.json("GET", CONFIG["youtube"]["videos_url"], params={
            "part": ",".join(CONFIG["youtube"]["video_parts"]), "id": ",".join(video_ids), "key": key,
        })
        return {video["id"]: video for video in result["items"]}

    def youtube_channels(self, channel_ids):
        if not channel_ids:
            return {}
        key, _ = self.aws.setting("youtube_api_key")
        result = self.http.json("GET", CONFIG["youtube"]["channels_url"], params={
            "part": ",".join(CONFIG["youtube"]["channel_parts"]),
            "id": ",".join(channel_ids), "maxResults": len(channel_ids), "key": key,
        })
        return {channel["id"]: channel for channel in result["items"]}

    def twitch_users(self, user_ids):
        if not user_ids:
            return {}
        result = self.twitch("GET", CONFIG["twitch"]["users_path"], params={"id": user_ids})
        return {user["id"]: user for user in result["data"]}


PLATFORMS = Platforms()
