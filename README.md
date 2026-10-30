# aws_streamer_tracker

Tracks subscribers, CCV, and streams for YouTube and Twitch streamers.

## Lambda packages

The Lambda entrypoints and shared ingestion package are in `aws/lambda/`. Install the build dependency and build all functions listed in [`scripts/build-config.yaml`](scripts/build-config.yaml):

```sh
python3 -m pip install -r requirements-build.txt
python3 scripts/build_lambda_zips.py
```

The config maps function names (ZIP filenames without `.zip`) to Python source files:

```yaml
functions:
  get-stream-stats: ../aws/lambda/get_stream_stats.py
```

Add an entry to build another function. Source paths are relative to the config file. Use `--config /path/to/build-config.yaml` to select another config. Terraform reads function names from the same config; permissions, routes, and schedules still need definitions for new functions.

The script writes to `terraform/artifacts/`, matching Terraform's default `lambda_artifact_directory`. The default config and output paths are relative to the repo, so it also works from other working directories. Use `--output-dir /path/to/artifacts` for a custom destination and set Terraform's `lambda_artifact_directory` to the same directory.

Each ZIP contains its handler at the archive root, the shared `tracker` package, runtime YAML, pinned runtime dependencies for Python 3.12/ARM64, and the RDS CA bundle. The builder uses pip or uv and requires internet access. Rerun it after changing sources or runtime YAML and before deploying with Terraform.

## Configuration

Platform URLs and runtime behavior are explicit in [`config/runtime.yaml`](config/runtime.yaml). Resource settings and schedules are in [`config/infrastructure.yaml`](config/infrastructure.yaml); GitHub OIDC/bootstrap settings are in [`config/bootstrap.yaml`](config/bootstrap.yaml). Existing Terraform and GitHub variable overrides remain supported. See the [configuration guide](docs/configuration.md) for precedence, deployment behavior, and secret handling.

## Subscriptions and ingestion

YouTube WebSub and Twitch EventSub registration, signed callbacks, EventBridge processing, and CCV polling are implemented. Follow the [subscription deployment guide](docs/subscriptions.md) to apply the additive database migration, create platform credentials, test callbacks, and enable schedules with `TRACKING_JOBS_ENABLED=true`. Schedules remain disabled by default until those prerequisites are ready.

The [stream inactivity watchdog](docs/stream-reconciliation.md) infers an end after ten minutes without valid live metadata and automatically recovers known streams when data returns. Apply migration 004 before deploying the updated Lambdas; the threshold is configured in runtime YAML.

The [channel updater](docs/channel-updates.md) discovers channel IDs from `streamer_info`, then refreshes tracked channel names, profile pictures, descriptions, and available statistics hourly, saving current values and history snapshots together. Newly added streamers/platform channels are picked up automatically. Its schedule and API settings are in YAML. Apply migration 002 before deploying it.

## CI/CD

[GitHub Actions](.github/workflows/deploy.yml) builds the Lambda ZIPs and validates Terraform on pull requests. Successful pushes to `main` deploy the application stack to the GitHub `dev` environment using AWS OIDC and shared S3 Terraform state. Manual deployments are also available from `main`.

Follow the [AWS deployment setup](docs/aws-deployment.md) to bootstrap the state bucket and deployment role, configure the GitHub environment variables, and migrate any existing local Terraform state before the first CI deployment.

## Database

Terraform provisions a private Amazon RDS PostgreSQL instance. Its master password is generated and stored by RDS in Secrets Manager. Apply [`db/schema.sql`](db/schema.sql), followed by all numbered files in [`db/migrations`](db/migrations), with a PostgreSQL client that can reach the private database (for example, through a VPN, bastion host, or an approved migration job). For an existing database, apply only outstanding migrations in order.

Platform channel IDs and platform video IDs are each unique across their entire table. Apply [migration 003](db/migrations/003_unique_platform_ids.sql) to enforce this on an existing database; see [duplicate checks and migration instructions](docs/database-constraints.md). UUID primary keys and existing ownership/history are preserved.

For local administration, the [database access helper](docs/database-access.md) starts the configured EC2 helper, opens a Session Manager tunnel, and launches your saved DBeaver connection. Source its Bash wrapper from `.bashrc` to use `streamer_db`; non-secret defaults are in [`config/database-access.yaml`](config/database-access.yaml).

An optional [VTuber seed](db/seeds/README.md) populates `streamer_info` with sourced channel IDs and generation names. Its SQL is safe to rerun and preserves existing records; review its roster coverage and field conventions before importing.

Lambda functions receive database resource references through the environment bindings in runtime YAML. The shared runtime retrieves credentials from Secrets Manager and uses the configured TLS mode, defaulting to certificate and hostname verification.

The dedicated VPC includes a NAT gateway so VPC-attached Lambda functions can call AWS, YouTube, and Twitch APIs. The NAT gateway incurs AWS charges.
