"""Read-only EC2 monitor. Recovery and all email are handled by Actions."""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import jwt
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
STATE = Path("/state/incidents.json")
EC2 = boto3.client("ec2", region_name="ap-south-1")
SSM = boto3.client("ssm", region_name="ap-south-2")
IDS = os.environ["INSTANCE_IDS"].split(",")
REPO = os.environ["GITHUB_REPOSITORY"]
APP = os.environ["GITHUB_APP_ID"]
INSTALL = os.environ["GITHUB_INSTALLATION_ID"]
BASE = "https://api.github.com"

def session():
    key = SSM.get_parameter(Name="/vj-recovery/github-app-key", WithDecryption=True)["Parameter"]["Value"]
    now = int(time.time())
    token = jwt.encode({"iat":now-60,"exp":now+540,"iss":APP}, key, algorithm="RS256")
    headers = {"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
    r = requests.post(f"{BASE}/app/installations/{INSTALL}/access_tokens",headers=headers,timeout=30)
    r.raise_for_status()
    s = requests.Session()
    s.headers.update(headers)
    s.headers["Authorization"] = "Bearer " + r.json()["token"]
    return s

def save(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)

def monitor_once(state):
    response = EC2.describe_instances(InstanceIds=IDS)
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            iid = instance["InstanceId"]
            status = instance["State"]["Name"]
            incident = state.get(iid)
            # One dispatch per stopped incident; no automatic retry storm after failure.
            # Keep the latch through pending -> running until EC2 has remained running 5 min.
            if status == "running":
                if incident:
                    incident.setdefault("running_since",time.time())
                    if time.time()-incident["running_since"] >= 300:
                        del state[iid]
                    save(state)
                continue
            if incident:
                incident.pop("running_since",None)
                save(state)
                continue
            if status != "stopped":
                continue
            detected = datetime.now(timezone.utc).isoformat()
            state[iid] = {"detected_at":detected,"dispatch":"pending"}
            save(state)  # Persist before POST; an ambiguous timeout must not dispatch repeatedly.
            try:
                s = session()
                r = s.post(f"{BASE}/repos/{REPO}/actions/workflows/recover-ec2.yml/dispatches",
                           json={"ref":"main","inputs":{"instance_id":iid,"detected_at":detected}},timeout=30)
                r.raise_for_status()
                state[iid]["dispatch"] = "accepted"
                logging.info("Recovery dispatched for %s",iid)
            except Exception:
                state[iid]["dispatch"] = "unknown_or_failed"
                logging.exception("Dispatch failed/uncertain for %s; check Actions before manual retry",iid)
            save(state)

if __name__ == "__main__":
    STATE.parent.mkdir(parents=True,exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    while True:
        try:
            monitor_once(state)
            logging.info("Monitoring %s", ",".join(IDS))
        except Exception:
            logging.exception("Monitor check failed")
        time.sleep(30)
