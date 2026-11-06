"""Upload source bundle; build on Hyderabad node and wait for SSM result."""
import json
import os
import shlex
import tarfile
import time
from pathlib import Path
import boto3

out=json.loads(Path("monitor-output.json").read_text())
iid=out["monitor_id"]["value"]
ids=out["instance_ids"]["value"]
bucket=os.environ["STATE_BUCKET"]
sha=os.environ["GITHUB_SHA"]
key=f"agent/{sha}.tgz"
with tarfile.open("/tmp/agent.tgz","w:gz") as t:
    t.add("agent",arcname="agent")
boto3.client("s3",region_name="ap-south-2").upload_file("/tmp/agent.tgz",bucket,key)
env="\n".join([
    "INSTANCE_IDS="+",".join(ids),
    "GITHUB_REPOSITORY="+os.environ["GITHUB_REPOSITORY"],
    "GITHUB_APP_ID="+os.environ["GH_APP_ID"],
    "GITHUB_INSTALLATION_ID="+os.environ["GH_INSTALLATION_ID"],
])+"\n"
# Only nonsecret config is written; the container retrieves the App key from SSM.
script="\n".join([
    "set -eu",
    "cloud-init status --wait",
    "systemctl is-active docker",
    "mkdir -p /opt/vj-recovery/state",
    f"aws s3 cp {shlex.quote('s3://'+bucket+'/'+key)} /tmp/agent.tgz --region ap-south-2",
    "tar -xzf /tmp/agent.tgz -C /opt/vj-recovery",
    "printf %s "+shlex.quote(env)+" > /opt/vj-recovery/agent.env",
    "docker build -t vj-recovery:"+sha+" /opt/vj-recovery/agent",
    "docker rm -f vj-recovery-agent 2>/dev/null || true",
    "docker run -d --name vj-recovery-agent --restart unless-stopped --env-file /opt/vj-recovery/agent.env -v /opt/vj-recovery/state:/state --log-opt max-size=10m --log-opt max-file=3 vj-recovery:"+sha,
    "sleep 10",
    "test \"$(docker inspect -f '{{.State.Running}}' vj-recovery-agent)\" = true",
    "docker logs --tail 20 vj-recovery-agent",
])
ssm=boto3.client("ssm",region_name="ap-south-2")
for attempt in range(60):
    info=ssm.describe_instance_information(Filters=[{"Key":"InstanceIds","Values":[iid]}])["InstanceInformationList"]
    if info and info[0]["PingStatus"]=="Online":
        break
    time.sleep(10)
else:
    raise TimeoutError("Hyderabad node did not become SSM Online")
command=ssm.send_command(InstanceIds=[iid],DocumentName="AWS-RunShellScript",Parameters={"commands":[script],"executionTimeout":["1200"]})["Command"]["CommandId"]
for attempt in range(150):
    try:
        result=ssm.get_command_invocation(CommandId=command,InstanceId=iid)
    except ssm.exceptions.InvocationDoesNotExist:
        time.sleep(10)
        continue
    if result["Status"]=="Success":
        print(result["StandardOutputContent"])
        break
    if result["Status"] not in ("Pending","InProgress","Delayed"):
        print(result["StandardOutputContent"])
        print(result["StandardErrorContent"])
        raise RuntimeError("SSM deployment failed: "+result["Status"])
    time.sleep(10)
else:
    raise TimeoutError("SSM deployment did not finish")
