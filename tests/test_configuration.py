from copy import deepcopy
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import yaml

from tests import support
from tracker.configuration import CONFIG, environment, load_config
from tracker.platforms import Platforms, youtube_topic
from tracker.runtime import raw_body


ROOT = Path(__file__).resolve().parents[1]


class RuntimeConfigurationTests(unittest.TestCase):
    def test_youtube_statistics_cannot_be_omitted_from_video_requests(self):
        """Reject request configurations that would misclassify every stream as members-only.

        Args:
            self (RuntimeConfigurationTests): Test instance loading modified YAML.

        Returns:
            None: Assert membership classification cannot run without requesting statistics.
        """
        with self.assertRaisesRegex(ValueError, "youtube.video_parts must include"):
            self.load_modified(lambda config: config["youtube"]["video_parts"].remove("statistics"))

    def load_modified(self, modify):
        """Write a modified copy of runtime YAML and return the loader validation result.

        Args:
            self (RuntimeConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.
            modify (Callable[[dict[str, Any]], Any]): Callback mutating a copied runtime
                configuration; its return value is ignored.

        Returns:
            dict[str, Any]: Validated modified configuration; validation errors propagate to
                assertions.
        """
        config = deepcopy(CONFIG)
        modify(config)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.yaml"
            path.write_text(yaml.safe_dump(config))
            return load_config(path)

    def test_missing_setting_fails_before_processing_events(self):
        """Verify missing required settings fail validation before event handling begins.

        Args:
            self (RuntimeConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        with self.assertRaisesRegex(ValueError, "Missing youtube settings"):
            self.load_modified(lambda config: config["youtube"].pop("hub_url"))

    def test_unknown_setting_is_not_silently_ignored(self):
        """Verify misspelled settings are rejected instead of silently ignored.

        Args:
            self (RuntimeConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        with self.assertRaisesRegex(ValueError, "Unknown youtube settings"):
            self.load_modified(lambda config: config["youtube"].update(renewal_fracton=0.5))

    def test_invalid_limits_and_urls_are_rejected(self):
        """Verify invalid numeric limits, provider batch sizes, and service URLs are rejected.

        Args:
            self (RuntimeConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        cases = [
            ("youtube", "renewal_fraction", 1),
            ("youtube", "max_refresh_interval_seconds", 0),
            ("youtube", "polling_batch_size", 51),
            ("twitch", "polling_batch_size", 101),
            ("aws", "eventbridge_batch_size", 11),
            ("channel_updates", "youtube_batch_size", 51),
            ("channel_updates", "twitch_batch_size", 101),
            ("http", "timeout_seconds", 0),
            ("http", "timeout_seconds", True),
            ("youtube", "hub_url", "http://example.com/"),
            ("twitch", "helix_url", "https://example.com?token=secret"),
        ]
        for section, key, value in cases:
            with self.subTest(setting=f"{section}.{key}", value=value), self.assertRaises(ValueError):
                self.load_modified(lambda config: config[section].update({key: value}))

    def test_endpoints_are_read_from_configuration(self):
        """Verify feed and metadata requests use endpoint overrides from runtime YAML.

        Args:
            self (RuntimeConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        settings = {**CONFIG["youtube"], "feed_url": "https://feed.example/videos", "videos_url": "https://api.example/videos"}
        http = MagicMock()
        http.json.return_value = {"items": []}
        with patch.dict(CONFIG, {"youtube": settings}):
            self.assertEqual(youtube_topic("a b"), "https://feed.example/videos?channel_id=a+b")
            Platforms(aws=support.aws_mock(), http=http).youtube_videos(["video"])
        self.assertEqual(http.json.call_args.args[1], "https://api.example/videos")

    def test_environment_binding_can_be_renamed_without_python_changes(self):
        """Verify a renamed YAML environment binding changes resource lookup without code edits.

        Args:
            self (RuntimeConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        with patch.dict(CONFIG["environment"], {"database_host": "CUSTOM_DATABASE_HOST"}), patch.dict(os.environ, {"CUSTOM_DATABASE_HOST": "db.example"}):
            self.assertEqual(environment("database_host"), "db.example")

    def test_configured_webhook_size_limit_is_enforced(self):
        """Verify webhook decoding accepts the configured boundary and rejects larger bodies.

        Args:
            self (RuntimeConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        with patch.dict(CONFIG["webhooks"], {"max_body_bytes": 3}):
            self.assertEqual(raw_body({"body": "abc"}), b"abc")
            with self.assertRaises(ValueError):
                raw_body({"body": "abcd"})


class DeploymentConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load the deployment-export script and checked-in infrastructure defaults once.

        Args:
            cls (type[DeploymentConfigurationTests]): Test-case class holding shared
                fixtures for all its tests.

        Returns:
            None: Store shared fixture state on the test class before its tests run.
        """
        spec = importlib.util.spec_from_file_location("configure_deployment", ROOT / "scripts/configure_deployment.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.config = yaml.safe_load((ROOT / "config/infrastructure.yaml").read_text())

    def test_yaml_defaults_reach_github_environment(self):
        """Verify YAML defaults become correctly serialized GitHub/Terraform environment values.

        Args:
            self (DeploymentConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        result = self.module.deployment_environment(self.config, {})
        self.assertEqual(result["AWS_REGION"], self.config["variables"]["aws_region"])
        self.assertEqual(result["TF_VAR_tracking_jobs_enabled"], "false")
        self.assertEqual(result["TF_VAR_alarm_topic_arn"], "")

    def test_explicit_zero_and_false_overrides_are_preserved(self):
        """Verify explicit zero and false settings take precedence over YAML defaults.

        Args:
            self (DeploymentConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        result = self.module.deployment_environment(self.config, {"TF_VAR_postgres_backup_retention_days": "0", "TF_VAR_tracking_jobs_enabled": "false"})
        self.assertEqual(result["TF_VAR_postgres_backup_retention_days"], "0")
        self.assertEqual(result["TF_VAR_tracking_jobs_enabled"], "false")

    def test_mismatched_authentication_and_terraform_regions_are_rejected(self):
        """Reject configurations that authenticate and deploy Terraform in different regions.

        Args:
            self (DeploymentConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        with self.assertRaisesRegex(ValueError, "must match"):
            self.module.deployment_environment(self.config, {"AWS_REGION": "eu-west-1", "TF_VAR_aws_region": "us-east-1"})

    def test_github_environment_line_injection_is_rejected(self):
        """Reject newline-bearing values that could inject additional GitHub environment entries.

        Args:
            self (DeploymentConfigurationTests): Test-case instance containing fixtures,
                injected adapters, and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        with self.assertRaisesRegex(ValueError, "single line"):
            self.module.deployment_environment(self.config, {"TF_VAR_project": "tracker\nAWS_REGION=unexpected"})
