# GitHub Actions deployment

The [workflow](../.github/workflows/deploy.yml) builds every Lambda in `scripts/build-config.yaml`, checks Terraform formatting, and validates both Terraform roots on pull requests and pushes to `main`. These checks do not need AWS credentials. After checks pass on `main`, the deployment job plans and applies the application Terraform in the GitHub `dev` environment. You can also run the workflow manually from `main`.

The deployment manages the entire application stack, including RDS, networking, IAM, API Gateway, schedules, and Lambda code. The ingestion functions implement WebSub/EventSub subscriptions, signed callbacks, stream reconciliation, and CCV polling. Database migration and platform-credential setup remain separate steps requiring access to the private RDS instance and SSM. Follow the [subscription deployment guide](subscriptions.md) before enabling schedules.

## One-time AWS setup

Use Terraform 1.14.6 and local AWS credentials for the target account with permission to create the bootstrap resources. The default target is `dev` in `us-east-1` for `sman11235/aws_streamer_tracker`.

```sh
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap plan -out=bootstrap.tfplan
terraform -chdir=terraform/bootstrap apply bootstrap.tfplan
terraform -chdir=terraform/bootstrap output github_environment_variables
```

The bootstrap creates a private, encrypted, versioned S3 bucket for application state and its lock file, a GitHub OIDC provider, and an IAM deployment role. It runs separately because the deployment needs these resources before it can authenticate or initialize its state. Keep the bootstrap's local `terraform.tfstate` in a secure, backed-up location; state and plan files are ignored by Git.

If the AWS account already has the GitHub OIDC provider, pass its ARN when creating the bootstrap plan:

```sh
terraform -chdir=terraform/bootstrap plan \
  -var='existing_oidc_provider_arn=arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com' \
  -out=bootstrap.tfplan
```

For another repository, set `github_repository`, `github_owner_id`, and `github_repository_id` when planning. The defaults match this repository's immutable GitHub IDs. The public GitHub API endpoint `https://api.github.com/repos/OWNER/REPO` returns the owner ID in `owner.id` and repository ID in `id`. For another region or project prefix, pass `-var='aws_region=REGION'` or `-var='project=PREFIX'`. Use the resulting output values in GitHub.

The deployment role trusts only this repository's `dev` environment through [GitHub OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws). Its state access is limited to the application state and lock objects. IAM role management is limited to the project's `dev` role prefix, and managed policy attachments are limited to the two AWS Lambda execution policies used by this stack. Infrastructure permissions cover the AWS services used by the application across the account; use a dedicated development account or adapt the policy to your account's resource restrictions.

## GitHub environment

In the repository's **Settings → Environments**, create `dev`. Restrict its deployment branches to `main`; the IAM trust uses the environment name, so this branch restriction controls which branches may assume the deployment role. Add required reviewers here if you want approval before each deployment.

Set these environment **variables** using the bootstrap output:

| Variable | Value |
| --- | --- |
| `AWS_ROLE_ARN` | Deployment role ARN |
| `AWS_REGION` | AWS region; defaults to `us-east-1` |
| `PROJECT` | Resource name prefix; defaults to `streamer-stats` |
| `TF_STATE_BUCKET` | Bootstrap S3 bucket name |
| `TF_STATE_KEY` | Application state key, normally `streamer-stats/dev/terraform.tfstate` |
| `POSTGRES_BACKUP_RETENTION_DAYS` | Optional automated backup retention; defaults to `1` day |
| `TRACKING_JOBS_ENABLED` | Set to `true` after the migration, SSM setup, and callback tests; defaults to `false` |
| `ALARM_TOPIC_ARN` | Optional existing SNS topic for ingestion alerts |

No AWS access keys need to be stored in GitHub. Only the deployment job can request an OIDC token; pull request checks do not assume the AWS role. Deployment jobs run one at a time and use [S3 state locking](https://developer.hashicorp.com/terraform/language/backend/s3). Each job applies the plan it just generated.

## Existing deployments and local Terraform

If the application was previously deployed using local Terraform state, migrate that state before enabling GitHub deployments. Run this from the same working copy that contains the existing application state, using the bootstrap bucket, key, and region:

```sh
terraform -chdir=terraform init -migrate-state \
  -backend-config='bucket=YOUR_STATE_BUCKET' \
  -backend-config='key=streamer-stats/dev/terraform.tfstate' \
  -backend-config='region=us-east-1'
```

For a new application deployment or a fresh clone, use the same command without `-migrate-state`. Local Terraform commands and GitHub must use the same bucket, key, region, project, and environment. Build the Lambda ZIPs before running a plan. The [provider lock files](../terraform/.terraform.lock.hcl) pin the AWS provider used locally and in CI; commit updates after intentionally upgrading it.

Create the existing SSM parameters listed in `terraform/variables.tf` before implementing or invoking handlers that read them. The workflow provisions infrastructure and uploads the ZIPs; it does not populate application secrets or apply `db/schema.sql`.

## RDS backup retention restrictions

The database defaults to one day of automated backup retention. AWS free-plan accounts can reject longer retention with `FreeTierRestrictionError`; the previous seven-day setting triggered this restriction. The [RDS backup retention documentation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.BackupRetention.html) explains the retention settings and their effect on backups.

Push the updated configuration to `main` to start a new deployment using one-day retention. An existing `POSTGRES_BACKUP_RETENTION_DAYS` GitHub environment variable must also be within your account's allowed range. To request longer retention on an account that supports it, set that GitHub environment variable or override `postgres_backup_retention_days` when running Terraform locally. Setting retention to `0` disables automated backups.

## OIDC authentication errors

Repositories created after July 15, 2026 use [immutable OIDC subjects](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims). This repository's deployment subject is:

```text
repo:sman11235@157008436/aws_streamer_tracker@1379103315:environment:dev
```

If deployment fails with `Not authorized to perform sts:AssumeRoleWithWebIdentity` after using an older bootstrap, update the AWS role's trust policy by applying the current bootstrap from the working copy containing its state:

```sh
terraform -chdir=terraform/bootstrap plan -out=bootstrap.tfplan
terraform -chdir=terraform/bootstrap apply bootstrap.tfplan
terraform -chdir=terraform/bootstrap output -raw github_oidc_subject
```

Check that the plan updates the deployment role's trust policy, then rerun the failed GitHub Actions deployment. The workflow's `AWS_ROLE_ARN` must identify this bootstrap's deployment role, its environment must be `dev`, and the OIDC audience must be `sts.amazonaws.com`. Repositories using the legacy subject format or a custom subject template must adapt the trust condition to match their actual token subject exactly.
