mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults        = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
    override_during = plan
  }
}

run "preserve_oidc_trust" {
  command = plan
  assert {
    condition     = aws_iam_openid_connect_provider.github[0].url == "https://token.actions.githubusercontent.com" && contains(aws_iam_openid_connect_provider.github[0].client_id_list, "sts.amazonaws.com") && length(aws_iam_openid_connect_provider.github[0].client_id_list) == 1
    error_message = "Bootstrap YAML must preserve the existing GitHub issuer and AWS audience."
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:sman11235@157008436/aws_streamer_tracker@1379103315:environment:dev"
    error_message = "The refactor must preserve the repository-ID-based GitHub OIDC subject."
  }
}

run "reuse_provider_override" {
  command = plan
  variables {
    project                    = "custom-tracker"
    existing_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  }
  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0 && aws_iam_role.deploy.name == "custom-tracker-dev-github-actions"
    error_message = "Explicit project/provider overrides must take precedence over bootstrap YAML."
  }
}
