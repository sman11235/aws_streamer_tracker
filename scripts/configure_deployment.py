#!/usr/bin/env python3
"""Export YAML deployment defaults and explicit overrides to GitHub Actions."""

import os
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def deployment_environment(config, environ):
    """Match Terraform precedence, preserving false and zero overrides."""
    result = {}
    for name, default in config["variables"].items():
        override = environ.get(f"TF_VAR_{name}")
        value = override if override not in {None, ""} else default
        if value is None:
            continue
        value = str(value).lower() if isinstance(value, bool) else str(value)
        if "\n" in value or "\r" in value:
            raise ValueError(f"Deployment setting {name} must contain a single line")
        result[f"TF_VAR_{name}"] = value
    region = environ.get("AWS_REGION") or result["TF_VAR_aws_region"]
    if region != result["TF_VAR_aws_region"]:
        raise ValueError("AWS_REGION and TF_VAR_aws_region must match")
    result["AWS_REGION"] = region
    return result


def main():
    with (ROOT / "config/infrastructure.yaml").open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    values = deployment_environment(config, os.environ)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as destination:
        destination.write("".join(f"{name}={value}\n" for name, value in values.items()))


if __name__ == "__main__":
    main()
