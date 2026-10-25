# GitHub Actions deployment

The [workflow](../.github/workflows/deploy.yml) builds every Lambda in `scripts/build-config.yaml`, checks Terraform formatting, and validates both Terraform roots on pull requests and pushes to `main`. These checks do not need AWS credentials. After checks pass on `main`, the deployment job plans and applies the application Terraform in the GitHub `dev` environment. You can also run the workflow manually from `main`.

The deployment manages the entire application stack, including RDS, networking, IAM, API Gateway, schedules, and Lambda code. Lambda source files are still empty placeholders, so deployed functions will not handle invocations until their handlers are implemented. Database schema application remains a separate step requiring access to the private RDS instance.

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

For another repository, region, or project prefix, pass `-var='github_repository=owner/repo'`, `-var='aws_region=REGION'`, or `-var='project=PREFIX'` when planning. Use the resulting output values in GitHub.

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
