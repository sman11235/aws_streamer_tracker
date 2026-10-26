"""AWS adapters, bounded HTTP requests, and API Gateway response handling."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tracker.configuration import CONFIG, environment


LOG = logging.getLogger("tracker")
LOG.setLevel(CONFIG["logging"]["level"])


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class ApiError(RuntimeError):
    def __init__(self, status, retry_after=None):
        # Never include a URL/query or response body: they can contain credentials.
        super().__init__(f"Platform API returned HTTP {status}")
        self.status = status
        self.retry_after = CONFIG["http"]["default_retry_after_seconds"] if retry_after is None else retry_after


class HTTP:
    def request(self, method, url, *, params=None, form=None, payload=None, headers=None):
        if params:
            url += "?" + urlencode(params, doseq=True)
        settings = CONFIG["http"]
        headers = {"User-Agent": settings["user_agent"], **(headers or {})}
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(url, data=body, headers=headers, method=method), timeout=settings["timeout_seconds"]) as response:
                return response.read(settings["max_response_bytes"])
        except HTTPError as error:
            delay = error.headers.get("Retry-After", settings["default_retry_after_seconds"])
            try:
                delay = max(settings["min_retry_after_seconds"], min(int(delay), settings["max_retry_after_seconds"]))
            except (TypeError, ValueError):
                delay = settings["default_retry_after_seconds"]
            raise ApiError(error.code, delay) from None
        except (URLError, TimeoutError, OSError):
            raise ApiError(503) from None

    def json(self, method, url, **kwargs):
        body = self.request(method, url, **kwargs)
        return json.loads(body) if body else {}


class AWS:
    def __init__(self):
        self.clients = {}
        self.parameters = {}
        self.secrets = {}

    def client(self, service):
        if service not in self.clients:
            import boto3
            from botocore.config import Config

            self.clients[service] = boto3.client(
                service, config=Config(
                    connect_timeout=CONFIG["aws"]["connect_timeout_seconds"],
                    read_timeout=CONFIG["aws"]["read_timeout_seconds"],
                    retries={"max_attempts": CONFIG["aws"]["max_retry_attempts"]},
                )
            )
        return self.clients[service]

    def parameter(self, name, version=None):
        key = f"{name}:{version}" if version is not None else name
        cached = self.parameters.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        result = self.client("ssm").get_parameter(Name=key, WithDecryption=True)["Parameter"]
        value = (result["Value"], result["Version"])
        self.parameters[key] = (time.monotonic() + CONFIG["aws"]["cache_ttl_seconds"], value)
        return value

    def setting(self, env_name, version=None):
        return self.parameter(environment(env_name), version)

    def database_secret(self):
        arn = environment("database_secret_arn")
        cached = self.secrets.get(arn)
        if not cached or cached[0] <= time.monotonic():
            result = self.client("secretsmanager").get_secret_value(SecretId=arn)
            cached = (time.monotonic() + CONFIG["aws"]["cache_ttl_seconds"], json.loads(result["SecretString"]))
            self.secrets[arn] = cached
        return cached[1]

    def publish(self, provider, details):
        entries = [
            {
                "EventBusName": environment("event_bus_name"),
                "Source": CONFIG["events"]["source_prefix"] + provider,
                "DetailType": CONFIG["events"]["detail_type"],
                "Detail": json.dumps(detail, separators=(",", ":")),
            }
            for detail in details
        ]
        batch_size = CONFIG["aws"]["eventbridge_batch_size"]
        for offset in range(0, len(entries), batch_size):
            result = self.client("events").put_events(Entries=entries[offset:offset + batch_size])
            if result.get("FailedEntryCount", 0) or any(
                entry.get("ErrorCode") for entry in result.get("Entries", [])
            ):
                raise RuntimeError("EventBridge did not accept every event")


AWS_CLIENT = AWS()
HTTP_CLIENT = HTTP()


def raw_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        result = base64.b64decode(body, validate=True)
    else:
        result = body.encode("utf-8")
    if len(result) > CONFIG["webhooks"]["max_body_bytes"]:
        raise ValueError("Webhook body too large")
    return result


def response(status=204, body=""):
    return {
        "statusCode": status,
        "headers": {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"},
        "body": body,
    }


def has_time(context, started, reserve=None):
    reserve = CONFIG["jobs"]["remaining_time_reserve_seconds"] if reserve is None else reserve
    if context is not None:
        return context.get_remaining_time_in_millis() > reserve * 1000
    return time.monotonic() - started < CONFIG["jobs"]["local_time_budget_seconds"]
