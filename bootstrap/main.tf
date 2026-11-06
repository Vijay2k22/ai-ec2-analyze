terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" { region = "ap-south-2" }
variable "github_repo" { description = "Exact OWNER/REPOSITORY" }
variable "oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN, or empty to create one"
  default     = ""
}
data "aws_caller_identity" "me" {}
locals { prefix = "vj-recovery" }
resource "aws_s3_bucket" "state" {
  bucket = "${local.prefix}-${data.aws_caller_identity.me.account_id}-state"
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.oidc_provider_arn == "" ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
resource "aws_iam_role" "deploy" {
  name = "vj-recovery-deploy"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = var.oidc_provider_arn != "" ? var.oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:Vijay2k22@119030242/ec2-github-recovery@1435457169:ref:refs/heads/main"
    } }
  }] })
}
# Lab provisioning role: broad EC2/ELB control, with IAM limited to project roles.
resource "aws_iam_role_policy" "deploy" {
  role = aws_iam_role.deploy.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ec2:*", "elasticloadbalancing:*", "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations"], Resource = "*" },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:*::parameter/aws/service/ami-amazon-linux-latest/*" },
    { Effect = "Allow", Action = "iam:CreateServiceLinkedRole", Resource = "arn:aws:iam::${data.aws_caller_identity.me.account_id}:role/aws-service-role/elasticloadbalancing.amazonaws.com/*", Condition = { StringEquals = { "iam:AWSServiceName" = "elasticloadbalancing.amazonaws.com" } } },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = ["arn:aws:ssm:ap-south-2::document/AWS-RunShellScript", "arn:aws:ec2:ap-south-2:${data.aws_caller_identity.me.account_id}:instance/*"] },
    { Effect = "Allow", Action = ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:UntagRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:ListInstanceProfilesForRole", "iam:TagInstanceProfile", "iam:UntagInstanceProfile"], Resource = ["arn:aws:iam::${data.aws_caller_identity.me.account_id}:role/vj-recovery-*", "arn:aws:iam::${data.aws_caller_identity.me.account_id}:instance-profile/vj-recovery-*"] },
    { Effect = "Allow", Action = "iam:PassRole", Resource = "arn:aws:iam::${data.aws_caller_identity.me.account_id}:role/vj-recovery-monitor", Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } },
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = aws_s3_bucket.state.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.state.arn}/*" }
  ] })
}
output "state_bucket" { value = aws_s3_bucket.state.id }
output "deploy_role_arn" { value = aws_iam_role.deploy.arn }
