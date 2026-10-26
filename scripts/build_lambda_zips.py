#!/usr/bin/env python3
"""Package each Lambda module into the ZIP expected by Terraform."""

import argparse
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "scripts" / "build-config.yaml",
        help="YAML build config (default: scripts/build-config.yaml in this repo).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "terraform" / "artifacts",
        help="Destination directory (default: terraform/artifacts in this repo).",
    )
    parser.add_argument(
        "--skip-dependencies", action="store_true",
        help="Omit runtime dependencies for local ZIP inspection only; do not deploy these ZIPs.",
    )
    args = parser.parse_args()

    try:
        import yaml
    except ImportError:
        parser.error(
            "PyYAML is required. Install it with: "
            f"python3 -m pip install -r {ROOT / 'requirements-build.txt'}"
        )

    config_path = args.config.resolve()
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        parser.error(f"Could not read build config {config_path}: {error}")

    functions = config.get("functions") if isinstance(config, dict) else None
    if not isinstance(functions, dict) or not functions:
        parser.error("Build config must contain a non-empty 'functions' mapping.")

    sources = {}
    for name, source_path in functions.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            parser.error(f"Invalid function name: {name!r}")
        if not isinstance(source_path, str) or not source_path.strip():
            parser.error(f"Function {name!r} must specify a Python source file path.")
        source = config_path.parent / source_path
        if source.suffix != ".py":
            parser.error(f"Function {name!r} must specify a .py source file: {source}")
        if not source.is_file():
            parser.error(f"Missing Lambda source: {source}")
        sources[name] = source

    common_files = {}
    infrastructure = config.get("infrastructure_config")
    if infrastructure:
        with (config_path.parent / infrastructure).open(encoding="utf-8") as source:
            deployment = yaml.safe_load(source)
        if deployment["lambda"]["runtime"] != "python" + config["python_version"]:
            parser.error("Build python_version must match infrastructure lambda.runtime")
        architecture = deployment["lambda"]["architectures"]
        platform_arch = {"arm64": "aarch64", "x86_64": "x86_64"}
        if len(architecture) != 1 or architecture[0] not in platform_arch:
            parser.error("Specify one supported Lambda architecture")
        expected = platform_arch[architecture[0]]
        if not config["pip_platform"].endswith("_" + expected) or not config["uv_platform"].startswith(expected + "-"):
            parser.error("Build wheel platforms must match infrastructure lambda.architectures")
    runtime_config = config.get("runtime_config")
    if runtime_config:
        runtime_path = config_path.parent / runtime_config
        if not runtime_path.is_file():
            parser.error(f"Missing runtime config: {runtime_path}")
        # Run exactly the same validation as a cold Lambda invocation.
        spec = importlib.util.spec_from_file_location("tracker_configuration", ROOT / "aws/lambda/tracker/configuration.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        runtime_settings = module.load_config(runtime_path)
        if config.get("rds_ca_bundle_url") and config["rds_ca_bundle_archive_path"] != "tracker/" + runtime_settings["database"]["ca_bundle_filename"]:
            parser.error("RDS certificate archive path must match the runtime CA bundle filename")
        common_files["tracker/config.yaml"] = runtime_path.read_bytes()
    shared = config.get("shared_directory")
    if shared:
        shared = config_path.parent / shared
        if not shared.is_dir() or not (shared / "__init__.py").is_file():
            parser.error("shared_directory must be a Python package directory")
        for path in sorted(shared.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                common_files[f"{shared.name}/{path.relative_to(shared).as_posix()}"] = path.read_bytes()
    ca_url = config.get("rds_ca_bundle_url")
    if ca_url:
        if not ca_url.startswith("https://truststore.pki.rds.amazonaws.com/"):
            parser.error("rds_ca_bundle_url must use the AWS RDS HTTPS trust store")
        try:
            with urlopen(ca_url, timeout=config["download_timeout_seconds"]) as bundle:
                common_files[config["rds_ca_bundle_archive_path"]] = bundle.read()
        except OSError as error:
            parser.error(f"Could not download the RDS CA bundle: {error}")

    with TemporaryDirectory(prefix="lambda-dependencies-") as staging:
        requirements = config.get("runtime_requirements")
        if requirements and not args.skip_dependencies:
            requirements = config_path.parent / requirements
            if not requirements.is_file():
                parser.error(f"Missing runtime requirements: {requirements}")
            if importlib.util.find_spec("pip"):
                command = [
                    sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-compile", "--only-binary=:all:", "--platform", config["pip_platform"],
                    "--implementation", config["python_implementation"], "--python-version", config["python_version"], "--target", staging,
                    "-r", str(requirements),
                ]
            elif shutil.which("uv"):
                command = [
                    "uv", "pip", "install", "--python", sys.executable,
                    "--only-binary=:all:", "--python-platform", config["uv_platform"],
                    "--python-version", config["python_version"], "--target", staging, "-r", str(requirements),
                ]
            else:
                parser.error("Install pip or uv to package runtime dependencies")
            subprocess.run(command, check=True)
            for path in sorted(Path(staging).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    name = path.relative_to(staging).as_posix()
                    if name in common_files:
                        parser.error(f"Dependency overwrites shared source: {name}")
                    common_files[name] = path.read_bytes()

        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, source in sources.items():
            if source.name in common_files:
                parser.error(f"Handler overwrites a dependency or shared file: {source.name}")
            destination = args.output_dir / f"{name}.zip"
            files = {**common_files, source.name: source.read_bytes()}
            with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
                for archive_name, data in sorted(files.items()):
                    # Stable metadata prevents uploads caused only by checkout times.
                    info = ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
            print(f"Built {destination}")


if __name__ == "__main__":
    main()
