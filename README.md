# aws_streamer_tracker

Tracks subscribers, CCV, and streams for YouTube and Twitch streamers.

## Lambda packages

The Lambda source files are in `aws/lambda/`. Install the build dependency and build all functions listed in [`scripts/build-config.yaml`](scripts/build-config.yaml):

```sh
python3 -m pip install -r requirements-build.txt
python3 scripts/build_lambda_zips.py
```

The config maps function names (ZIP filenames without `.zip`) to Python source files:

```yaml
functions:
  get-stream-stats: ../aws/lambda/get_stream_stats.py
```

Add an entry to build another function. Source paths are relative to the config file. Use `--config /path/to/build-config.yaml` to select another config. Terraform's function definitions are managed separately.

The script writes to `terraform/artifacts/`, matching Terraform's default `lambda_artifact_directory`. The default config and output paths are relative to the repo, so it also works from other working directories. Use `--output-dir /path/to/artifacts` for a custom destination and set Terraform's `lambda_artifact_directory` to the same directory.

Each ZIP contains its corresponding Python module at the archive root. The source files are currently empty placeholders; add a `handler` function before invoking the Lambdas. The script packages only these modules; dependency packaging must be added when dependencies are introduced. Rerun the script after changing the source files and before deploying with Terraform.

## CI/CD

[GitHub Actions](.github/workflows/deploy.yml) builds the Lambda ZIPs and validates Terraform on pull requests. Successful pushes to `main` deploy the application stack to the GitHub `dev` environment using AWS OIDC and shared S3 Terraform state. Manual deployments are also available from `main`.

Follow the [AWS deployment setup](docs/aws-deployment.md) to bootstrap the state bucket and deployment role, configure the GitHub environment variables, and migrate any existing local Terraform state before the first CI deployment.

## Database

Terraform provisions a private Amazon RDS PostgreSQL instance. Its master password is generated and stored by RDS in Secrets Manager. The database schema is in [`terraform/schema.sql`](terraform/schema.sql); apply it after RDS is available with a PostgreSQL client that can reach the private database (for example, through a VPN, bastion host, or an approved migration job).

Lambda functions receive `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_SECRET_ARN`, and `DATABASE_SSLMODE`. Their deployment ZIPs must include a PostgreSQL driver and use these settings instead of the removed DynamoDB table environment variables.

The dedicated VPC includes a NAT gateway so VPC-attached Lambda functions can call AWS, YouTube, and Twitch APIs. The NAT gateway incurs AWS charges.
