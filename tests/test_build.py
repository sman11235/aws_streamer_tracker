from contextlib import redirect_stdout
from hashlib import sha256
import importlib.util
from io import StringIO
import os
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import yaml


class BuildTests(unittest.TestCase):
    def test_packaged_runtime_config_loads_without_source_checkout(self):
        """Verify an extracted ZIP loads its bundled YAML without access to the source checkout.

        Args:
            self (BuildTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("lambda_builder", root / "scripts/build_lambda_zips.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with TemporaryDirectory(prefix="tracker-config-build-") as directory:
            directory = Path(directory)
            runtime = yaml.safe_load((root / "config/runtime.yaml").read_text())
            runtime["twitch"]["message_max_age_seconds"] = 123
            (directory / "runtime.yaml").write_text(yaml.safe_dump(runtime))
            (directory / "handler.py").write_text("from tracker.configuration import CONFIG\n")
            (directory / "build.yaml").write_text(yaml.safe_dump({
                "shared_directory": str(root / "aws/lambda/tracker"),
                "runtime_config": "runtime.yaml", "functions": {"example": "handler.py"},
            }))
            args = [str(root / "scripts/build_lambda_zips.py"), "--config", str(directory / "build.yaml"), "--output-dir", str(directory / "output")]
            with patch.object(sys, "argv", args), redirect_stdout(StringIO()):
                builder.main()
            with ZipFile(directory / "output/example.zip") as archive:
                archive.extractall(directory / "extracted")
            env = {**os.environ, "PYTHONPATH": str(directory / "extracted")}
            env.pop("TRACKER_CONFIG_FILE", None)
            result = subprocess.run([sys.executable, "-c", "from tracker.configuration import CONFIG; print(CONFIG['twitch']['message_max_age_seconds'])"], cwd=directory, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "123")

    def test_custom_config_shared_package_and_source_timestamps(self):
        """Verify custom build inputs are packaged and source timestamp changes preserve the ZIP hash.

        Args:
            self (BuildTests): Test-case instance containing fixtures, injected adapters,
                and assertion helpers.

        Returns:
            None: Assertions complete without returning a value; failures raise a test
                assertion error.
        """
        script = Path(__file__).resolve().parents[1] / "scripts/build_lambda_zips.py"
        spec = importlib.util.spec_from_file_location("lambda_builder", script)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with TemporaryDirectory(prefix="tracker-build-test-") as directory:
            root = Path(directory)
            source = root / "handler.py"
            source.write_text("def handler(event, context):\n    return event\n")
            shared = root / "common"
            shared.mkdir()
            (shared / "__init__.py").write_text("VALUE = 1\n")
            config = root / "build.yaml"
            config.write_text("shared_directory: common\nfunctions:\n  custom-function: handler.py\n")
            args = [str(script), "--config", str(config), "--output-dir", str(root / "output")]
            with patch.object(sys, "argv", args), redirect_stdout(StringIO()):
                builder.main()
            artifact = root / "output/custom-function.zip"
            before = sha256(artifact.read_bytes()).hexdigest()
            os.utime(source, (1700000000, 1700000000))
            with patch.object(sys, "argv", args), redirect_stdout(StringIO()):
                builder.main()
            self.assertEqual(sha256(artifact.read_bytes()).hexdigest(), before)
            with ZipFile(artifact) as archive:
                self.assertEqual(set(archive.namelist()), {"handler.py", "common/__init__.py"})
