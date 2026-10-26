#!/usr/bin/env python3
"""Export YAML deployment defaults and explicit overrides to GitHub Actions."""

import os
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def deployment_environment(config, environ):
    """Resolve YAML deployment defaults and explicit process overrides.

    Args:
        config (Mapping[str, Any]): Parsed infrastructure YAML containing Terraform
            defaults under variables.
        environ (Mapping[str, str]): Process settings containing optional TF_VAR_*
            overrides and AWS_REGION.

    Returns:
        dict[str, str]: Serialized Terraform/AWS environment entries suitable for a
            GitHub environment file.

    Raises:
        ValueError: A value contains a newline or AWS/Terraform regions disagree.
    """
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
    """Append resolved deployment defaults and overrides to the GitHub GITHUB_ENV file.

    Returns:
        None: Append resolved deployment variables to the file named by GITHUB_ENV.
    """
    with (ROOT / "config/infrastructure.yaml").open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    values = deployment_environment(config, os.environ)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as destination:
        destination.write("".join(f"{name}={value}\n" for name, value in values.items()))


if __name__ == "__main__":
    main()
