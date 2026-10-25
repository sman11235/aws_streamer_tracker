#!/usr/bin/env python3
"""Package each Lambda module into the ZIP expected by Terraform."""

import argparse
from pathlib import Path
import re
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

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        destination = args.output_dir / f"{name}.zip"
        with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
            # Keep hashes stable across fresh CI checkouts of unchanged sources.
            info = ZipInfo(source.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())
        print(f"Built {destination}")


if __name__ == "__main__":
    main()
