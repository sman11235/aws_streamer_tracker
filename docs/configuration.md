# Configuration

All checked-in configuration is public and must contain no secret values.

| File | Consumer | Settings |
| --- | --- | --- |
| [`config/runtime.yaml`](../config/runtime.yaml) | All Lambdas; Terraform reads shared resource bindings | Platform URLs/API paths, leases, retries, timeouts, batch sizes, channel updates, token caching, webhook limits, thumbnails, TLS, logging, event routing, and environment variable names |
| [`config/infrastructure.yaml`](../config/infrastructure.yaml) | Application Terraform and GitHub deployment setup | Resource defaults, SSM parameter names, Lambda resources, networking, RDS, callback paths, schedules, delivery retries, queue retention, and alarms |
| [`config/bootstrap.yaml`](../config/bootstrap.yaml) | Bootstrap Terraform | GitHub repository identity, OIDC issuer/audience, AWS region, project, environment, and optional existing OIDC provider ARN |
| [`scripts/build-config.yaml`](../scripts/build-config.yaml) | ZIP builder; Terraform reads function names | Handler sources, shared package, runtime configuration, dependency lockfile, Python/wheel targets, and RDS certificate download |
| [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) | GitHub Actions | Triggers, runners, tool versions, CI PostgreSQL service, deployment environment, and GitHub variable bindings |

## Runtime configuration

The builder validates runtime YAML and includes it as `tracker/config.yaml` in every ZIP. Lambda cold starts validate and load that copy; no runtime YAML download occurs. Source checkouts read the original file. `TRACKER_CONFIG_FILE=/absolute/path/runtime.yaml` selects a complete alternative file for local runs. Missing, unknown, or invalid settings fail validation; provider batch maxima remain enforced in code.

Changing runtime YAML requires rebuilding and deploying the affected ZIPs. Python no longer reads `WEBSUB_LEASE_SECONDS`, `SUBSCRIPTION_BATCH_SIZE`, or `DATABASE_SSLMODE`; use `youtube.requested_lease_seconds`, `subscriptions.batch_size`, and `database.ssl_mode` instead. TLS defaults to certificate and hostname verification.

`youtube.max_refresh_interval_seconds` is 432000 (five days). WebSub renewal is due at the earlier of that age since verification or `youtube.renewal_fraction` of the granted lease. This controls each subscription's deadline; the maintenance job still runs every ten minutes.

API field names, XML namespaces, signature algorithms, status codes, SQL, and IAM permission definitions remain code because they define protocols and application behavior.

## Infrastructure and overrides

Terraform reads YAML directly with `yamldecode(file(...))`. Input variables default to null, selecting the corresponding value under `variables` in infrastructure YAML. Existing explicit `-var`, `.tfvars`, and `TF_VAR_*` inputs override those defaults. Bootstrap follows the same rule using its separate YAML file.

GitHub Actions runs `scripts/configure_deployment.py` before AWS authentication and Terraform initialization. The workflow's existing GitHub variables override YAML defaults through their `TF_VAR_*` bindings. Explicit `false` and `0` overrides are preserved. `AWS_ROLE_ARN`, `TF_STATE_BUCKET`, and `TF_STATE_KEY` still come from the GitHub environment because bootstrap generates those account-specific values.

Generated database addresses, the database secret ARN, the event bus name, and API Gateway callback base URLs remain Terraform resource references. Runtime YAML names their environment variables, and Terraform populates them. Callback paths are shared by API routes, Lambda environment, and Terraform outputs.

Schedules are under `schedules.jobs` in infrastructure YAML: subscription maintenance every ten minutes, stream polling every two minutes, and [channel/history updates](channel-updates.md) hourly UTC. For daily midnight UTC maintenance use `cron(0 0 * * ? *)`; for five-minute maintenance use `rate(5 minutes)`. Schedule changes require Terraform deployment. Schedules remain disabled by default until `tracking_jobs_enabled` is enabled. A stored `retry_at` does not itself invoke a Lambda.

Keep bootstrap/application project, region, and environment consistent with the GitHub deployment environment and OIDC subject. Bootstrap has a separate configuration because it manages its own state and deployment trust.

## Secrets and packaging

Only SSM parameter **names** belong in YAML. Signing secrets, Twitch credentials, and the YouTube API key stay in SSM SecureString parameters. Database credentials stay in the RDS-managed Secrets Manager secret. YAML changes do not create or rotate secret values.

The builder validates that its Python version and wheel architecture match infrastructure YAML. Keep the GitHub Actions Python version aligned too. Adding a function in build YAML also adds its Terraform function name; IAM permissions, routes, and schedules still need definitions for new functions.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/build_lambda_zips.py
terraform fmt -check -recursive terraform
terraform -chdir=terraform validate
terraform -chdir=terraform/bootstrap validate
terraform -chdir=terraform test
terraform -chdir=terraform/bootstrap test
```
