import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from mcp.server.fastmcp import FastMCP

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text())
DB = Path(os.environ.get("STATE_DB_PATH", CFG["database"]))
MCP = FastMCP("6F EC2 tools")
ACTIONS = {"start_ec2": "stopped", "restart_apache": "apache", "limit_apache_cpu": "cpu"}

def aws(service, region=None):
    return boto3.client(service, region_name=region or CFG["region"], config=Config(
        connect_timeout=10, read_timeout=90 if service == "bedrock-runtime" else 30,
        retries={"mode": "standard", "total_max_attempts": 1}))

@contextmanager
def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def initialize():
    with db() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY, instance TEXT, kind TEXT, status TEXT, action TEXT,
            evidence TEXT, result TEXT DEFAULT '{}', version INTEGER DEFAULT 1,
            created REAL, deadline REAL, attempts INTEGER DEFAULT 0, attempted REAL DEFAULT 0)""")
        # Import old records once; preserve their IDs and original evidence.
        legacy = connection.execute("SELECT name FROM sqlite_master WHERE name='incidents'").fetchone()
        if legacy:
            for row in connection.execute("SELECT * FROM incidents").fetchall():
                action = "start_ec2" if row["action"] == "start_ec2_instance" else row["action"]
                connection.execute("INSERT OR IGNORE INTO cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    row["id"], row["instance_id"], ACTIONS.get(action, "apache"),
                    "recovered" if row["status"] == "recovered" else "review", action,
                    row["evidence"], row["result"] or '{}', 1, row["created_at"], 0,
                    row["attempts"], 0))

def records():
    with db() as connection:
        connection.execute("UPDATE cases SET status='review' WHERE status IN ('pending','approved') AND deadline < ?", (time.time(),))
        return [dict(row) for row in connection.execute("SELECT * FROM cases ORDER BY created DESC")]

def instance(instance_id):
    if instance_id not in CFG["instances"]:
        raise ValueError("Instance is outside the paired scope.")
    return aws("ec2").describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]

def cpu(instance_id):
    end = datetime.now(timezone.utc)
    points = aws("cloudwatch").get_metric_statistics(
        Namespace="AWS/EC2", MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=end-timedelta(minutes=25), EndTime=end, Period=300,
        Statistics=["Average"])["Datapoints"]
    points = sorted(points, key=lambda point: point["Timestamp"])[-CFG["cpu_points"]:]
    fresh = bool(points) and (end-points[-1]["Timestamp"]).total_seconds() < 600
    high = fresh and len(points) == CFG["cpu_points"] and all(p["Average"] >= CFG["cpu_threshold"] for p in points)
    return {"sustained_high": high, "fresh": fresh, "threshold": CFG["cpu_threshold"],
            "points": [{"utc": p["Timestamp"].isoformat(), "average": p["Average"]} for p in points]}

@MCP.tool()
def inspect_instance(instance_id: str) -> dict:
    """Read paired EC2 specifications, configuration, health and CPU metrics."""
    item = instance(instance_id)
    checks = aws("ec2").describe_instance_status(InstanceIds=[instance_id], IncludeAllInstances=True)["InstanceStatuses"]
    targets = aws("elbv2").describe_target_health(TargetGroupArn=CFG["target_group"], Targets=[{"Id": instance_id, "Port": 80}])["TargetHealthDescriptions"]
    result = {"instance": instance_id, "state": item["State"]["Name"], "type": item["InstanceType"],
              "architecture": item.get("Architecture"), "ami": item["ImageId"], "az": item["Placement"]["AvailabilityZone"],
              "private_ip": item.get("PrivateIpAddress"), "public_ip": item.get("PublicIpAddress"),
              "vpc": item.get("VpcId"), "subnet": item.get("SubnetId"), "security_groups": item["SecurityGroups"],
              "iam_profile": item.get("IamInstanceProfile"), "metadata_options": item.get("MetadataOptions"),
              "state_reason": item.get("StateReason"), "state_transition": item.get("StateTransitionReason"),
              "tags": {t["Key"]: t["Value"] for t in item.get("Tags", [])}, "checks": checks,
              "target": targets[0]["TargetHealth"] if targets else {}, "checked_utc": datetime.now(timezone.utc).isoformat()}
    for name, getter in {
        "cpu": lambda: cpu(instance_id),
        "type_details": lambda: aws("ec2").describe_instance_types(InstanceTypes=[item["InstanceType"]])["InstanceTypes"][0],
        "volumes": lambda: aws("ec2").describe_volumes(Filters=[{"Name": "attachment.instance-id", "Values": [instance_id]}])["Volumes"],
    }.items():
        try:
            result[name] = getter()
        except Exception as error:
            result[name] = {"error": str(error)}
    return json.loads(json.dumps(result, default=str))

@MCP.tool()
def stop_history(instance_id: str) -> dict:
    """Read recent stop API events. Caller identity does not prove human intent."""
    item = instance(instance_id)
    response = aws("cloudtrail").lookup_events(
        LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": instance_id}],
        StartTime=datetime.now(timezone.utc)-timedelta(days=7), MaxResults=50)
    events = []
    for entry in response["Events"]:
        event = json.loads(entry["CloudTrailEvent"])
        if entry["EventName"] in {"StopInstances", "RebootInstances", "StartInstances"}:
            identity = event.get("userIdentity", {})
            events.append({"time": str(entry["EventTime"]), "event": entry["EventName"],
                           "caller": identity.get("arn", entry.get("Username")), "identity_type": identity.get("type"),
                           "invoked_by": identity.get("invokedBy"), "user_agent": event.get("userAgent"),
                           "source_ip": event.get("sourceIPAddress"), "error": event.get("errorCode")})
    return {"state_reason": item.get("StateReason"), "transition": item.get("StateTransitionReason"),
            "events": events, "truncated": bool(response.get("NextToken")),
            "note": "Match event time to this stop. No matching event means cause unknown, not system failure."}

def send(instance_id, commands, comment):
    return aws("ssm").send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands, "executionTimeout": ["30"]}, TimeoutSeconds=30,
        Comment=comment)["Command"]["CommandId"]

def command_result(instance_id, command_id):
    client = aws("ssm")
    for _ in range(15):
        try:
            result = client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            if result["Status"] not in {"Pending", "InProgress", "Delayed", "Cancelling"}:
                return {"status": result["Status"], "stdout": result.get("StandardOutputContent", "")[:6500],
                        "stderr": result.get("StandardErrorContent", "")[:1000], "command_id": command_id}
        except client.exceptions.InvocationDoesNotExist:
            pass
        time.sleep(2)
    return {"status": "pending", "command_id": command_id}

@MCP.tool()
def diagnose_service(instance_id: str) -> dict:
    """Read Apache status, HTTP, current CPU samples, top processes and service/system logs."""
    if instance(instance_id)["State"]["Name"] != "running":
        return {"error": "Stopped instance: service diagnostics unavailable."}
    result = command_result(instance_id, send(instance_id, CFG["diagnostic_commands"], "6F read-only diagnostics"))
    for line in result.get("stdout", "").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"service", "http", "apache_cpu", "host_cpu"}:
            result[key] = value
    return result

def eligible(item):
    tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
    if any(tags.get(key) != value for key, value in CFG["tags"].items()):
        raise ValueError("Recovery blocked: allowlist/maintenance tags do not permit it.")

def validate_action(instance_id, action):
    if action not in ACTIONS:
        raise ValueError("Unsupported action.")
    item = instance(instance_id)
    eligible(item)
    if action == "start_ec2":
        if item["State"]["Name"] != "stopped":
            raise ValueError("Instance is not stopped.")
        return
    if item["State"]["Name"] != "running":
        raise ValueError("Service action requires running EC2.")
    evidence = diagnose_service(instance_id)
    if evidence.get("status") != "Success":
        raise ValueError("Service evidence is unavailable.")
    if action == "restart_apache" and evidence.get("service") not in {"inactive", "failed"}:
        raise ValueError("Apache is not inactive or failed; no restart permitted.")
    if action == "limit_apache_cpu":
        share = float(evidence.get("apache_cpu", "nan"))
        if not cpu(instance_id)["sustained_high"] or not math.isfinite(share) or share < CFG["apache_cpu_share"]:
            raise ValueError("Sustained CPU and Apache contribution are not confirmed.")

@MCP.tool()
def propose_recovery(instance_id: str, action: str, reason: str) -> dict:
    """Validate evidence and create an approval request; NEVER executes a change."""
    validate_action(instance_id, action)
    now = time.time()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cases WHERE instance=? AND kind=? AND status!='recovered' ORDER BY created DESC LIMIT 1",
                                 (instance_id, ACTIONS[action])).fetchone()
        if row and row["status"] in {"pending", "approved", "executing"}:
            return dict(row)
        if row and json.loads(row["result"]).get("uncertain"):
            raise ValueError("Prior execution uncertain: verify existing incident before proposing again.")
        case_id = row["id"] if row else str(uuid.uuid4())
        if row:
            connection.execute("UPDATE cases SET status='pending', action=?, evidence=?, version=version+1, deadline=? WHERE id=?",
                               (action, reason, now+CFG["approval_seconds"], case_id))
        else:
            connection.execute("INSERT INTO cases(id,instance,kind,status,action,evidence,created,deadline) VALUES(?,?,?,'pending',?,?,?,?)",
                               (case_id, instance_id, ACTIONS[action], action, reason, now, now+CFG["approval_seconds"]))
        return dict(connection.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())

def decide(case_id, version, approve, operator):
    if type(approve) is not bool or not operator:
        raise ValueError("Authenticated operator decision required.")
    with db() as connection:
        changed = connection.execute("UPDATE cases SET status=?, result=? WHERE id=? AND version=? AND status='pending' AND deadline>?",
            ("approved" if approve else "rejected", json.dumps({"operator": operator, "decided_utc": datetime.now(timezone.utc).isoformat()}), case_id, version, time.time())).rowcount
        if changed != 1:
            raise ValueError("Request changed, expired or was already decided. Refresh it.")

@MCP.tool()
def execute_approved(case_id: str, version: int) -> dict:
    """Execute only the exact stored human-approved action, once."""
    row = next((r for r in records() if r["id"] == case_id), None)
    if not row or row["status"] != "approved" or row["version"] != version:
        raise ValueError("Valid approval required.")
    validate_action(row["instance"], row["action"])
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute("SELECT MAX(attempted) AS t FROM cases WHERE instance=?", (row["instance"],)).fetchone()["t"] or 0
        if time.time()-latest < CFG["cooldown_seconds"]:
            raise ValueError("Instance recovery cooldown is active.")
        changed = connection.execute("UPDATE cases SET status='executing', attempts=attempts+1, attempted=? WHERE id=? AND version=? AND status='approved' AND deadline>? AND attempts<?",
            (time.time(), case_id, version, time.time(), CFG["max_attempts"])).rowcount
        if changed != 1:
            raise ValueError("Approval expired, consumed, or attempt limit reached.")
    audit = json.loads(row["result"])
    try:
        if row["action"] == "start_ec2":
            aws("ec2").start_instances(InstanceIds=[row["instance"]])
            result = {"submitted": True}
        else:
            command = "systemctl restart httpd" if row["action"] == "restart_apache" else f"systemctl set-property --runtime httpd.service CPUQuota={CFG['apache_cpu_quota']}%"
            result = {"submitted": True, "command_id": send(row["instance"], [command], "6F approved recovery")}
    except Exception as error:
        result = {"uncertain": True, "error": str(error)}
    result = {**audit, **result}
    with db() as connection:
        connection.execute("UPDATE cases SET result=? WHERE id=?", (json.dumps(result), case_id))
    return result

@MCP.tool()
def verify_recovery(case_id: str) -> dict:
    """Check actual health; never re-execute recovery. Uncertainty needs manual review."""
    row = next((r for r in records() if r["id"] == case_id), None)
    if not row or row["status"] == "recovered":
        raise ValueError("No unresolved incident to verify.")
    result = json.loads(row["result"])
    state = inspect_instance(row["instance"])
    service = diagnose_service(row["instance"]) if state["state"] == "running" else {}
    healthy = (state["state"] == "running" and state["target"].get("State") == "healthy"
               and service.get("service") == "active" and service.get("http") == "200")
    healthy = healthy and all(c.get(k, {}).get("Status") == "ok" for c in state["checks"] for k in ("InstanceStatus", "SystemStatus")) and bool(state["checks"])
    if row["kind"] == "cpu":
        metric = state.get("cpu", {})
        healthy = healthy and metric.get("fresh", False) and len(metric.get("points", [])) >= CFG["cpu_points"]
        healthy = healthy and all(p["average"] < CFG["cpu_threshold"] for p in metric.get("points", []))
        healthy = healthy and float(service.get("host_cpu", "nan")) < CFG["cpu_threshold"]
    verification = {"recovered": healthy, "state": state["state"], "alb": state["target"],
                    "service": service.get("service"), "http": service.get("http"), "host_cpu": service.get("host_cpu"),
                    "checked_utc": datetime.now(timezone.utc).isoformat(),
                    "note": "Health verified; this does not independently prove the cause of recovery."}
    status = "recovered" if healthy else row["status"]
    verification["resolution"] = "Verified after recorded action" if row["attempted"] else "Resolved outside this agent"
    result["verification"] = verification
    if healthy:
        result.pop("uncertain", None)
    with db() as connection:
        connection.execute("UPDATE cases SET status=?, result=? WHERE id=? AND version=? AND status=? AND attempted=?", (status, json.dumps(result), case_id, row["version"], row["status"], row["attempted"]))
    return {"status": status, **verification}

if __name__ == "__main__":
    initialize()
    MCP.run(transport="stdio")
