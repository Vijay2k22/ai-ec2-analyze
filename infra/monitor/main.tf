provider "aws" { region = "ap-south-2" }
variable "state_bucket" { type = string }
data "terraform_remote_state" "web" {
  backend = "s3"
  config  = { bucket = var.state_bucket, key = "web/terraform.tfstate", region = "ap-south-2" }
}
data "aws_caller_identity" "me" {}
data "aws_ssm_parameter" "ami" { name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" }
resource "aws_vpc" "monitor" {
  cidr_block           = "10.72.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "vj-recovery-monitor" }
}
resource "aws_internet_gateway" "monitor" { vpc_id = aws_vpc.monitor.id }
resource "aws_subnet" "monitor" {
  vpc_id                  = aws_vpc.monitor.id
  cidr_block              = "10.72.1.0/24"
  map_public_ip_on_launch = true
}
resource "aws_route_table" "monitor" {
  vpc_id = aws_vpc.monitor.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.monitor.id
  }
}
resource "aws_route_table_association" "monitor" {
  subnet_id      = aws_subnet.monitor.id
  route_table_id = aws_route_table.monitor.id
}
resource "aws_security_group" "monitor" {
  name_prefix = "vj-recovery-monitor-"
  vpc_id      = aws_vpc.monitor.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_iam_role" "monitor" {
  name               = "vj-recovery-monitor"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.monitor.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "monitor" {
  role = aws_iam_role.monitor.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ec2:DescribeInstances", Resource = "*" },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:ap-south-2:${data.aws_caller_identity.me.account_id}:parameter/vj-recovery/github-app-key" },
    { Effect = "Allow", Action = "s3:GetObject", Resource = "arn:aws:s3:::${var.state_bucket}/agent/*" }
  ] })
}
resource "aws_iam_instance_profile" "monitor" {
  name = "vj-recovery-monitor"
  role = aws_iam_role.monitor.name
}
resource "aws_instance" "monitor" {
  ami                    = data.aws_ssm_parameter.ami.value
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.monitor.id
  vpc_security_group_ids = [aws_security_group.monitor.id]
  iam_instance_profile   = aws_iam_instance_profile.monitor.name
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  root_block_device {
    encrypted   = true
    volume_size = 12
  }
  user_data  = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y docker
    systemctl enable --now docker amazon-ssm-agent
    mkdir -p /opt/vj-recovery/state
  EOT
  tags       = { Name = "vj-recovery-monitor", Project = "vj-recovery" }
  depends_on = [aws_route_table_association.monitor, aws_iam_role_policy_attachment.ssm, aws_iam_role_policy.monitor]
}
output "monitor_id" { value = aws_instance.monitor.id }
output "instance_ids" { value = data.terraform_remote_state.web.outputs.instance_ids }
