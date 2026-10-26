"""Load explicit non-secret YAML settings from the ZIP or source checkout."""

import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


REQUIRED_KEYS = {
    "logging": "level",
    "http": "user_agent timeout_seconds max_response_bytes default_retry_after_seconds min_retry_after_seconds max_retry_after_seconds",
    "aws": "connect_timeout_seconds read_timeout_seconds max_retry_attempts cache_ttl_seconds eventbridge_batch_size",
    "database": "port connect_timeout_seconds ssl_mode ca_bundle_filename",
    "jobs": "remaining_time_reserve_seconds local_time_budget_seconds",
    "channel_updates": "youtube_batch_size twitch_batch_size",
    "subscriptions": "batch_size pending_retry_seconds verification_timeout_seconds retry_base_seconds retry_max_seconds retry_exponent_cap",
    "youtube": "hub_url feed_url videos_url channels_url channel_parts video_parts requested_lease_seconds renewal_fraction max_accepted_lease_seconds polling_batch_size thumbnail_priority",
    "twitch": "helix_url token_url token_validation_url subscriptions_path streams_path users_path token_validation_interval_seconds token_expiry_margin_seconds authorization_attempts subscription_page_size polling_batch_size message_max_age_seconds message_future_tolerance_seconds revocation_retry_seconds terminal_subscription_statuses thumbnail_width thumbnail_height",
    "webhooks": "max_body_bytes",
    "events": "source_prefix detail_type",
    "environment": "event_bus_name database_host database_port database_name database_secret_arn database_ca_bundle youtube_secret twitch_secret twitch_client_id twitch_client_secret youtube_api_key youtube_callback twitch_callback",
}


def load_config(path):
    """Fail at startup for missing settings and invalid operational limits."""
    with Path(path).open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    if not isinstance(config, dict):
        raise ValueError("Runtime configuration must be a YAML mapping")
    sections = REQUIRED_KEYS
    if config.keys() - sections.keys():
        raise ValueError("Unknown runtime configuration section")
    for section in sections:
        if not isinstance(config.get(section), dict) or not config[section]:
            raise ValueError(f"Missing runtime configuration section: {section}")
        missing = set(REQUIRED_KEYS[section].split()) - config[section].keys()
        if missing:
            raise ValueError(f"Missing {section} settings: {', '.join(sorted(missing))}")
        unknown = config[section].keys() - set(REQUIRED_KEYS[section].split())
        if unknown:
            raise ValueError(f"Unknown {section} settings: {', '.join(sorted(unknown))}")
    # Integers are tunable, but provider API maxima and protocol constraints are
    # enforced in code so a typo cannot produce invalid requests.
    for section in sections:
        for name, value in config[section].items():
            if name.endswith(("_seconds", "_bytes", "_size", "_attempts", "_cap", "_width", "_height")) or (section == "database" and name == "port"):
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{section}.{name} must be a positive integer")
            if name.endswith("_url"):
                if not isinstance(value, str):
                    raise ValueError(f"{section}.{name} must be a URL string")
                parts = urlsplit(value)
                if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                    raise ValueError(f"{section}.{name} must be an HTTPS URL without credentials, query or fragment")
    for section, name, maximum in (
        ("aws", "eventbridge_batch_size", 10), ("youtube", "polling_batch_size", 50),
        ("twitch", "polling_batch_size", 100), ("twitch", "subscription_page_size", 100),
        ("channel_updates", "youtube_batch_size", 50), ("channel_updates", "twitch_batch_size", 100),
    ):
        if config[section][name] > maximum:
            raise ValueError(f"{section}.{name} exceeds the provider maximum {maximum}")
    fraction = config["youtube"]["renewal_fraction"]
    if type(fraction) not in {int, float} or not 0 < fraction < 1:
        raise ValueError("youtube.renewal_fraction must be between 0 and 1")
    if config["subscriptions"]["pending_retry_seconds"] > config["subscriptions"]["verification_timeout_seconds"]:
        raise ValueError("Pending retry must not exceed the verification timeout")
    if config["http"]["min_retry_after_seconds"] > config["http"]["max_retry_after_seconds"]:
        raise ValueError("Minimum HTTP retry delay exceeds maximum")
    if config["database"]["ssl_mode"] not in {"verify-full", "require"}:
        raise ValueError("database.ssl_mode must be verify-full or require")
    for name, value in config["environment"].items():
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]+", value):
            raise ValueError(f"Invalid environment binding: {name}")
    if len(set(config["environment"].values())) != len(config["environment"]):
        raise ValueError("Environment bindings must be unique")
    for section, name in (("youtube", "video_parts"), ("youtube", "channel_parts"), ("youtube", "thumbnail_priority"), ("twitch", "terminal_subscription_statuses")):
        value = config[section][name]
        if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
            raise ValueError(f"{section}.{name} must be a non-empty list of strings")
    if not config["twitch"]["helix_url"].endswith("/"):
        raise ValueError("twitch.helix_url must end with a slash")
    for section, names in {
        "logging": "level", "http": "user_agent", "database": "ca_bundle_filename",
        "events": "source_prefix detail_type", "twitch": "subscriptions_path streams_path users_path",
    }.items():
        for name in names.split():
            if not isinstance(config[section][name], str) or not config[section][name]:
                raise ValueError(f"{section}.{name} must be a non-empty string")
    if Path(config["database"]["ca_bundle_filename"]).name != config["database"]["ca_bundle_filename"]:
        raise ValueError("database.ca_bundle_filename must be a filename without directories")
    return config


def environment(name, default=None):
    """Resolve a logical resource/secret binding to its process environment."""
    variable = CONFIG["environment"][name]
    return os.environ[variable] if default is None else os.environ.get(variable, default)


_packaged = Path(__file__).with_name("config.yaml")
_source = _packaged if _packaged.is_file() else Path(__file__).resolve().parents[3] / "config/runtime.yaml"
CONFIG = load_config(os.environ.get("TRACKER_CONFIG_FILE", _source))
