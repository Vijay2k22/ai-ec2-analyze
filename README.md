# EC2 recovery through three GitHub Actions pipelines

New, separate lab: two Apache EC2s + ALB in Mumbai; one read-only monitoring EC2 in Hyderabad.
A stopped instance triggers a GitHub Actions recovery workflow that starts it and sends SMTP emails.
No SNS, no LLM/model is used: this is a rule-based monitoring and recovery agent.

## Before starting

Use Ubuntu/WSL with AWS CLI v2, Terraform >=1.10 (workflows use 1.14.0), Git, and optionally GitHub CLI (`gh`).
Authenticate AWS CLI with an account allowed to create networking, IAM, S3 and SSM resources:

```bash
aws configure
aws sts get-caller-identity
```

These files create NEW resources and NEW state; they do not adopt your previous lab.
Do not copy the previous terraform.tfstate into this project. Existing resources require a separate import/migration plan.
This HTTP-only learning lab uses public subnets for outbound connectivity, no SSH ingress, ALB-only inbound HTTP on web nodes,
and no inbound rules on the monitor. EC2, ALB and public IPv4 incur ongoing AWS charges.
The bootstrap deployment role has broad EC2/ELB provisioning access; use a sandbox account.
Its IAM mutations are restricted to vj-recovery-* roles/profiles, but this is not a hardened production permission boundary.

## 1. Create repository and upload files

Extract ZIP so `.github` is inside the project folder. Work from:
`~/projects/ec2-github-recovery`.
Create an EMPTY private GitHub repository named `ec2-github-recovery` (without README).

```bash
cd ~/projects/ec2-github-recovery
git init -b main
git add .
git commit -m "Add Terraform and GitHub Actions EC2 recovery lab"
git remote add origin https://github.com/YOUR_OWNER/ec2-github-recovery.git
git push -u origin main
```

Initial push may start the agent workflow before configuration exists; that first run can fail safely at configuration/authentication.
Never commit PEM keys, passwords, state files or tfvars containing secrets.

## 2. One-time Terraform bootstrap (local)

Set YOUR_OWNER to your actual GitHub username or organization:

```bash
export REPO="YOUR_OWNER/ec2-github-recovery"
cd ~/projects/ec2-github-recovery/bootstrap
terraform init
terraform plan -var="github_repo=$REPO" -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
terraform output
```

If your AWS account already has `token.actions.githubusercontent.com` as an IAM OIDC provider,
include its ARN in the PLAN command (do not create a second provider):

```bash
terraform plan -var="github_repo=$REPO"   -var="oidc_provider_arn=arn:aws:iam::YOUR_ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"   -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

Save bootstrap state securely outside Git; it owns the state bucket and deployment role.
Bootstrap must finish before either deployment workflow can authenticate.

## 3. Add initial GitHub repository variables

Repository -> Settings -> Secrets and variables -> Actions -> Variables -> New repository variable:

| Variable | Value |
|---|---|
| DEPLOY_ROLE_ARN | deploy_role_arn from bootstrap output |
| STATE_BUCKET | state_bucket from bootstrap output |
| SMTP_HOST | smtp.gmail.com (for Gmail) |
| SMTP_PORT | 587 |
| ALERT_EMAIL | Your recipient email address |

GitHub -> Settings -> Actions -> General: allow GitHub Actions and the official actions used in these workflows.
Only dispatch workflows against the main branch; the OIDC trust is restricted to main.
Keep write access to this private repository limited to trusted maintainers.

## 4. Run Mumbai infrastructure pipeline

Repository -> Actions -> `01 - Mumbai Apache infrastructure` -> Run workflow -> main -> Run workflow.
The job runs Initialize -> Validate -> Plan -> Apply. This starter applies automatically when manually dispatched.
Inspect the plan log; for environments needing approval, split plan/apply with an environment approval before apply.

Open the completed run's Summary. Copy these values into repository Variables:

| Variable | Source |
|---|---|
| ALLOWED_INSTANCE_IDS | Two IDs, comma-separated, no spaces |
| TARGET_GROUP_ARN | Mumbai target group ARN |
| RECOVERY_ROLE_ARN | Restricted runtime role ARN |

ALB_URL is also displayed. Open that URL and confirm Apache works.
Wait until BOTH Mumbai targets are Healthy before starting recovery tests.
If web resources are replaced later, update the three Variables and rerun pipeline 02 to refresh agent IDs.

## 5. Configure pipeline email (no SNS)

For a Gmail sender: enable Google 2-Step Verification and create an App Password if your account supports it.
Use the app password, not your normal Gmail password. If unavailable, use your approved SMTP provider supporting STARTTLS on 587.
The code authenticates using username/password; it does not implement Microsoft OAuth SMTP.

Repository -> Settings -> Secrets and variables -> Actions -> Secrets -> New repository secret:

| Secret | Value |
|---|---|
| SMTP_USER | Sender Gmail address / SMTP username |
| SMTP_PASSWORD | Gmail app password / SMTP password |

The sender must be a real address accepted by your SMTP provider.
The receiving email is configured in ALERT_EMAIL. SMTP acceptance does not guarantee inbox delivery; check spam.

## 6. Create and install GitHub App for the Hyderabad agent

GitHub account Settings -> Developer settings -> GitHub Apps -> New GitHub App:

- Name: a unique name, e.g. `vj-ec2-recovery-YOUR_OWNER`.
- Homepage URL: `https://github.com/YOUR_OWNER/ec2-github-recovery`.
- Uncheck Active under Webhook; no webhook is needed.
- Repository permissions: **Actions: Read and write**. Metadata read is automatic. No Contents write permission.
- Where can this GitHub App be installed? **Only on this account**.
- Create GitHub App. Record its **App ID**.
- Generate a private key and keep the downloaded `.pem` outside the repository.
- Left menu Install App -> Install -> Only select repositories -> select `ec2-github-recovery`.
- On the installation configuration page, the URL ends with `/installations/NUMBER`; record NUMBER as Installation ID.

Add repository Variables:

| Variable | Value |
|---|---|
| GH_APP_ID | App ID, not Client ID |
| GH_INSTALLATION_ID | Installation ID |

Store the private key as an AWS SSM SecureString in Hyderabad. Run locally, updating the path:

```bash
aws ssm put-parameter   --region ap-south-2   --name /vj-recovery/github-app-key   --type SecureString   --value file:///home/YOUR_LINUX_USER/private-keys/YOUR_APP_KEY.pem   --overwrite
```

Use the default SSM encryption key for this starter; a customer-managed KMS key needs additional kms:Decrypt permission.
The App key never goes into Terraform, GitHub variables, user-data or Docker images.

## 7. Run Hyderabad agent deployment pipeline

Actions -> `02 - Hyderabad monitor and agent` -> Run workflow -> main.
It creates the monitor EC2, waits for SSM Online, uploads a source bundle to a private S3 prefix,
builds the Docker image on that EC2 and starts it with restart policy `unless-stopped`.
No ECR is needed for this starter. Future changes under agent/ automatically rerun pipeline 02 on main.

Deployment logs show the EC2 ID and recent agent logs. For full logs:
AWS Console -> Systems Manager -> Session Manager -> Start session -> monitor node:

```bash
sudo docker ps
sudo docker logs --tail 100 vj-recovery-agent
```

The monitor checks the two configured Mumbai IDs every 30 seconds. It cannot start/stop any EC2.
Its GitHub App installation token is minted from the SecureString key for each dispatch.

## 8. Test the full flow

1. Confirm BOTH ALB targets Healthy and agent logs show monitoring.
2. AWS Console -> EC2 -> Mumbai -> select ONE `vj-recovery-web-*` -> Instance state -> Stop instance.
   Use **Stop**, not Terminate. The monitor dispatches after EC2 reaches `stopped`, not while `stopping`.
3. GitHub Actions -> `03 - EC2 recovery and email` should appear. GitHub runner queueing may add delay.
4. Observe named jobs:

| Stage | Behavior / email |
|---|---|
| 1 - SHUTDOWN DETECTED | Validate allowlist + tags + stopped state; send shutdown email |
| 2 - STARTING | AWS StartInstances; send start initiated email |
| 3 - STARTED SUCCESSFULLY | Wait EC2 running + status checks + ALB healthy; send success email |
| 4 - FAILED | Only on failure; send job results and run URL |
| 5 - SUMMARY | Always record stage results in run summary |

Stages 1/2 email errors are visible as warnings and do not block recovery.
A stage 3 success-email error marks that job failed; failure email explains that verification/email logs must be checked.
No workflow can guarantee email delivery if the SMTP provider rejects credentials or is unavailable.
The success notification is sent only AFTER both EC2 checks and Apache ALB health pass.

The workflow receives only the instance ID and detected time; configured allowlist, tags and restricted IAM validate its scope.
Workflow concurrency serializes recovery for the same instance. A later duplicate revalidates state and cannot blindly restart it.

## 9. Updates and troubleshooting

- Edit agent logic: `agent/agent.py`; edit pipeline email subjects/body: `scripts/email_status.py`.
- Edit recovery stages: `.github/workflows/recover-ec2.yml`.
- Edit web resources: `infra/web/main.tf`, then manually run pipeline 01.
- Edit monitor resources: `infra/monitor/main.tf`, then pipeline 02 runs on main changes.
- Cancel a recovery? Inspect EC2 state; the API start request may already have been accepted.
- Agent dispatch HTTP 403/404: check App installation, repository Actions write permission, App/installation IDs and workflow file on main.
- SSM timeout: check instance profile, outbound internet, SSM agent and cloud-init logs.
- Recovery AccessDenied: check RECOVERY_ROLE_ARN and main-branch OIDC trust.
- IAM update propagation may briefly cause first-run AccessDenied; wait and rerun.

Duplicate protection persists `/opt/vj-recovery/state/incidents.json` across container deployments.
An incident rearms only after the instance remains running for five continuous minutes.
If dispatch times out or recovery fails, the incident is latched to avoid endless retries.
Check Actions and EC2 state, then manually dispatch pipeline 03 for a confirmed stopped instance.
To clear a stale latch deliberately after investigation, stop the agent, remove that single instance's entry from incidents.json,
and start the agent. Do not delete state blindly while recovery is in progress.
No external alert is possible if GitHub itself cannot be reached; the monitor records the error locally.
Service-only Apache failure while EC2 remains running is outside this project; terminated instances are not recreated by recovery.

## 10. Optional teardown (incurs destruction)

Stop/remove the monitor first so it cannot interfere. Using local AWS credentials with matching permissions,
initialize each root against the same backend then review its destroy plan:

```bash
cd ~/projects/ec2-github-recovery
export STATE_BUCKET="YOUR_STATE_BUCKET"
export TF_VAR_state_bucket="$STATE_BUCKET"
export TF_VAR_github_repo="YOUR_OWNER/ec2-github-recovery"
terraform -chdir=infra/monitor init -backend-config="bucket=$STATE_BUCKET" -backend-config="key=monitor/terraform.tfstate" -backend-config="region=ap-south-2" -backend-config="use_lockfile=true"
terraform -chdir=infra/monitor destroy
terraform -chdir=infra/web init -backend-config="bucket=$STATE_BUCKET" -backend-config="key=web/terraform.tfstate" -backend-config="region=ap-south-2" -backend-config="use_lockfile=true"
terraform -chdir=infra/web destroy
```

Delete SSM key and uninstall GitHub App if no longer used. Retain state bucket/bootstrap state as needed;
the versioned state bucket will not destroy while it contains versions.

## Files

- `.github/workflows/infrastructure.yml`: Mumbai deployment.
- `.github/workflows/deploy-agent.yml`: Hyderabad deployment and agent updates.
- `.github/workflows/recover-ec2.yml`: recovery stages and email.
- `bootstrap/main.tf`: S3 state bucket and main-branch OIDC deployment role.
- `infra/web/`: Mumbai VPC, web nodes, ALB and restricted recovery IAM.
- `infra/monitor/`: Hyderabad VPC, monitor node and read-only monitoring IAM.
- `agent/`: Python monitor, Dockerfile, requirements.
- `scripts/`: SSM deployment, recovery verification and SMTP email.
- `tests/`: instance-scope validation tests.

## Validation and sources

Local checks: Python compilation, seven unit tests, Terraform formatting and HCL parsing, and YAML parsing passed. Full terraform validate was blocked in this workspace by an AWS provider package checksum mismatch. No AWS plan/apply, GitHub dispatch or SMTP delivery has been executed here. Run terraform init and validate with an official provider download in your environment before deploying. AWS provisioning, GitHub dispatch and SMTP delivery require your account setup and a real end-to-end test.
Terraform provider versions resolve within 6.x on init; generated `.terraform.lock.hcl` files should be committed after your first successful init.
Actions use official major-version tags; pin reviewed commit SHAs for stricter supply-chain control.

- https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws
- https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- https://developer.hashicorp.com/terraform/language/backend/s3
- https://docs.aws.amazon.com/systems-manager/latest/userguide/walkthrough-cli.html
