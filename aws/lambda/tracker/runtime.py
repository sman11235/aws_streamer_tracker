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
    """Return the current UTC time as a timezone-aware datetime.

    Returns:
        datetime.datetime: Current UTC instant with timezone information.
    """
    return datetime.now(timezone.utc)


def timestamp(value):
    """Parse an ISO timestamp, accepting a trailing Z as UTC.

    Args:
        value (str): ISO timestamp containing an offset or trailing Z; naive times are
            rejected.

    Returns:
        datetime.datetime: Parsed timezone-aware instant.

    Raises:
        ValueError: The timestamp is malformed or has no timezone.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed


def fingerprint(*parts):
    """Return a deterministic SHA-256 key for JSON-serializable event fields.

    Args:
        *parts (tuple[Any, ...]): JSON-serializable field values whose order determines
            the event fingerprint.

    Returns:
        str: Hexadecimal SHA-256 digest of the serialized field tuple.
    """
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class ApiError(RuntimeError):
    def __init__(self, status, retry_after=None):
        """Store a platform HTTP status and its retry delay.

        Use the configured default delay when retry_after is omitted. Exception
        messages exclude URLs, response bodies, and credentials to keep logs safe.

        Args:
            self (ApiError): Exception instance receiving platform failure and retry
                metadata.
            status (int): HTTP status code returned by, or assigned to, the failed platform
                request.
            retry_after (int | None): Retry delay in seconds; None selects the configured
                default.

        Returns:
            None: Initialize the exception status, safe message, and retry delay in place.
        """
        # Never include a URL/query or response body: they can contain credentials.
        super().__init__(f"Platform API returned HTTP {status}")
        self.status = status
        self.retry_after = CONFIG["http"]["default_retry_after_seconds"] if retry_after is None else retry_after


class HTTP:
    def request(self, method, url, *, params=None, form=None, payload=None, headers=None):
        """Send an HTTP request and return bounded response bytes.

        Args:
            self (HTTP): HTTP adapter instance performing configured outbound requests.
            method (str): HTTP method, such as GET, POST, or DELETE.
            url (str): Destination URL before optional query parameters are appended.
            params (Mapping[str, Any] | None): Optional query fields; sequence values
                produce repeated keys.
            form (Mapping[str, Any] | None): Optional fields encoded as an
                application/x-www-form-urlencoded body.
            payload (dict[str, Any] | list[Any] | str | int | float | bool | None):
                Optional JSON body; None omits it, and a supplied value overrides form.
            headers (Mapping[str, str] | None): Optional request headers merged with the
                configured User-Agent.

        Returns:
            bytes: Response body bytes, read up to the configured size limit.

        Raises:
            ApiError: An HTTP error, network failure, or socket timeout occurs.
                Numeric Retry-After values are bounded by the configured limits.
        """
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
        """Send a request through request() and decode its JSON response.

        Return an empty dictionary for an empty body. Forward keyword arguments
        to request(); HTTP errors and JSON decoding failures propagate to callers.

        Args:
            self (HTTP): HTTP adapter instance performing configured outbound requests.
            method (str): HTTP method forwarded to request().
            url (str): Destination URL forwarded to request().
            **kwargs (dict[str, Any]): Optional request settings, such as params, form,
                payload, and headers.

        Returns:
            dict[str, Any] | list[Any] | str | int | float | bool | None: Decoded JSON
                object, array, scalar, or null; an empty body produces {}.
        """
        body = self.request(method, url, **kwargs)
        return json.loads(body) if body else {}


class AWS:
    def __init__(self):
        """Initialize per-instance SDK client, parameter, and secret caches.

        Args:
            self (AWS): AWS adapter instance owning SDK clients and parameter/secret caches.

        Returns:
            None: Initialize empty client, parameter, and secret caches on this adapter.
        """
        self.clients = {}
        self.parameters = {}
        self.secrets = {}

    def client(self, service):
        """Return a cached boto3 client for the named AWS service.

        Create the client on first use with configured socket timeouts and retry
        limits, allowing warm Lambda invocations to reuse it.

        Args:
            self (AWS): AWS adapter instance owning SDK clients and parameter/secret caches.
            service (str): boto3 service name, such as ssm, events, or secretsmanager.

        Returns:
            botocore.client.BaseClient: Reusable SDK client configured with this
                application's timeout and retry settings.
        """
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
        """Read a decrypted SSM parameter, optionally at a specific version.

        Args:
            self (AWS): AWS adapter instance owning SDK clients and parameter/secret caches.
            name (str): SSM parameter name to retrieve with decryption.
            version (int | None): Specific SSM version to retrieve; None selects the latest
                version.

        Returns:
            tuple[str, int]: Decrypted parameter value and its actual SSM version number.
        """
        key = f"{name}:{version}" if version is not None else name
        cached = self.parameters.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        result = self.client("ssm").get_parameter(Name=key, WithDecryption=True)["Parameter"]
        value = (result["Value"], result["Version"])
        self.parameters[key] = (time.monotonic() + CONFIG["aws"]["cache_ttl_seconds"], value)
        return value

    def setting(self, env_name, version=None):
        """Resolve a logical YAML environment binding and read its SSM parameter.

        Args:
            self (AWS): AWS adapter instance owning SDK clients and parameter/secret caches.
            env_name (str): Logical key in runtime YAML environment bindings whose value
                names an SSM parameter.
            version (int | None): Requested SSM parameter version, or None for the latest
                version.

        Returns:
            tuple[str, int]: Resolved parameter value and SSM version number.
        """
        return self.parameter(environment(env_name), version)

    def database_secret(self):
        """Return cached database credentials decoded from Secrets Manager JSON.

        Resolve the secret ARN through the configured environment binding and
        refresh the cache when its configured lifetime expires.

        Args:
            self (AWS): AWS adapter instance owning SDK clients and parameter/secret caches.

        Returns:
            dict[str, Any]: Decoded database secret containing username and password fields.
        """
        arn = environment("database_secret_arn")
        cached = self.secrets.get(arn)
        if not cached or cached[0] <= time.monotonic():
            result = self.client("secretsmanager").get_secret_value(SecretId=arn)
            cached = (time.monotonic() + CONFIG["aws"]["cache_ttl_seconds"], json.loads(result["SecretString"]))
            self.secrets[arn] = cached
        return cached[1]

    def publish(self, provider, details):
        """Publish normalized lifecycle details to the configured EventBridge bus.

        Send entries in configured batches. Return only after all batches are
        accepted; previously accepted entries may be redelivered after a retry.

        Args:
            self (AWS): AWS adapter instance owning SDK clients and parameter/secret caches.
            provider (str): Provider identifier appended to the event source prefix,
                normally youtube or twitch.
            details (Iterable[dict[str, Any]]): Normalized lifecycle-event detail mappings
                to serialize and publish.

        Returns:
            None: All batches were accepted; any failed entry raises instead of returning
                success.

        Raises:
            RuntimeError: EventBridge reports any individual entry failure.
        """
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
    """Recover original webhook bytes from an API Gateway request.

    Decode base64 bodies when indicated; otherwise encode the body as UTF-8.
    Raise ValueError for an invalid base64 value or an oversized decoded body.

    Args:
        event (dict[str, Any]): API Gateway HTTP event containing body and optional
            isBase64Encoded fields.

    Returns:
        bytes: Original request-body bytes suitable for signature verification.
    """
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        result = base64.b64decode(body, validate=True)
    else:
        result = body.encode("utf-8")
    if len(result) > CONFIG["webhooks"]["max_body_bytes"]:
        raise ValueError("Webhook body too large")
    return result


def response(status=204, body=""):
    """Build a plain-text API Gateway response with caching disabled.

    Args:
        status (int): HTTP response status code; defaults to 204.
        body (str): Plain-text response content, such as a provider verification
            challenge.

    Returns:
        dict[str, Any]: API Gateway response with statusCode, no-store text headers, and
            body.
    """
    return {
        "statusCode": status,
        "headers": {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"},
        "body": body,
    }


def has_time(context, started, reserve=None):
    """Return whether enough invocation time remains to begin more work.

    Use Lambda's remaining time and the configured reserve when context is
    present; otherwise compare elapsed monotonic time to the local budget.
    This check does not interrupt operations that are already running.

    Args:
        context (LambdaContext | None): AWS Lambda context exposing remaining
            milliseconds, or None for local execution.
        started (float): Monotonic start time used when there is no Lambda context.
        reserve (int | float | None): Seconds to reserve before timeout; None selects
            the YAML setting.

    Returns:
        bool: True when the applicable time budget permits starting another unit of
            work.
    """
    reserve = CONFIG["jobs"]["remaining_time_reserve_seconds"] if reserve is None else reserve
    if context is not None:
        return context.get_remaining_time_in_millis() > reserve * 1000
    return time.monotonic() - started < CONFIG["jobs"]["local_time_budget_seconds"]
