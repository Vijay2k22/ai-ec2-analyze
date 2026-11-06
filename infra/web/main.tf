provider "aws" { region = "ap-south-1" }
data "aws_caller_identity" "me" {}
data "aws_availability_zones" "available" { state = "available" }
data "aws_ssm_parameter" "ami" { name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" }
variable "github_repo" { type = string }
resource "aws_vpc" "web" {
  cidr_block           = "10.71.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "vj-recovery-web", Project = "vj-recovery" }
}
resource "aws_internet_gateway" "web" { vpc_id = aws_vpc.web.id }
resource "aws_subnet" "web" {
  count                   = 2
  vpc_id                  = aws_vpc.web.id
  cidr_block              = "10.71.${count.index + 1}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
}
resource "aws_route_table" "web" {
  vpc_id = aws_vpc.web.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.web.id
  }
}
resource "aws_route_table_association" "web" {
  count          = 2
  subnet_id      = aws_subnet.web[count.index].id
  route_table_id = aws_route_table.web.id
}
resource "aws_security_group" "alb" {
  name_prefix = "vj-recovery-alb-"
  vpc_id      = aws_vpc.web.id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "web" {
  name_prefix = "vj-recovery-web-"
  vpc_id      = aws_vpc.web.id
  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_instance" "web" {
  count                  = 2
  ami                    = data.aws_ssm_parameter.ami.value
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.web[count.index].id
  vpc_security_group_ids = [aws_security_group.web.id]
  metadata_options { http_tokens = "required" }
  root_block_device {
    encrypted   = true
    volume_size = 8
  }
  user_data  = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y httpd
    echo '<h1>Mumbai Apache ${count.index + 1}</h1>' > /var/www/html/index.html
    systemctl enable --now httpd
  EOT
  tags       = { Name = "vj-recovery-web-${count.index + 1}", Project = "vj-recovery", AutoRecover = "true" }
  depends_on = [aws_route_table_association.web]
}
resource "aws_lb" "web" {
  depends_on         = [aws_route_table_association.web]
  name               = "vj-recovery-web"
  load_balancer_type = "application"
  subnets            = aws_subnet.web[*].id
  security_groups    = [aws_security_group.alb.id]
}
resource "aws_lb_target_group" "web" {
  name     = "vj-recovery-web"
  port     = 80
  protocol = "HTTP"
  vpc_id   = aws_vpc.web.id
  health_check {
    path                = "/"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
}
resource "aws_lb_target_group_attachment" "web" {
  count            = 2
  target_group_arn = aws_lb_target_group.web.arn
  target_id        = aws_instance.web[count.index].id
  port             = 80
}
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}
resource "aws_iam_role" "recovery" {
  name = "vj-recovery-runtime"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = "arn:aws:iam::${data.aws_caller_identity.me.account_id}:oidc-provider/token.actions.githubusercontent.com" },
    Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = "repo:Vijay2k22@119030242/ec2-github-recovery@1435457169:ref:refs/heads/main" } }
  }] })
}
resource "aws_iam_role_policy" "recovery" {
  role = aws_iam_role.recovery.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "elasticloadbalancing:DescribeTargetHealth"], Resource = "*" },
    { Effect = "Allow", Action = "ec2:StartInstances", Resource = aws_instance.web[*].arn }
  ] })
}
output "instance_ids" { value = aws_instance.web[*].id }
output "target_group_arn" { value = aws_lb_target_group.web.arn }
output "alb_url" { value = "http://${aws_lb.web.dns_name}" }
output "recovery_role_arn" { value = aws_iam_role.recovery.arn }
