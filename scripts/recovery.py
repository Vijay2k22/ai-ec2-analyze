"""Executed inside recovery jobs; never accepts arbitrary instances or target groups."""
import argparse
import json
import os
import re
import time
from pathlib import Path
import boto3

def validate_instance(ec2, iid, allowed):
    if not re.fullmatch(r"i-[0-9a-f]{8,17}",iid) or iid not in allowed:
        raise ValueError("Instance ID is not in the configured lab allowlist")
    instance = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    tags = {t["Key"]:t["Value"] for t in instance.get("Tags",[])}
    if tags.get("Project") != "vj-recovery" or tags.get("AutoRecover") != "true":
        raise ValueError("Instance recovery tags do not match")
    return instance, tags.get("Name",iid)

def main():
    action = argparse.ArgumentParser()
    action.add_argument("action",choices=["validate","start","verify"])
    args = action.parse_args()
    iid = os.environ["INSTANCE_ID"]
    ec2 = boto3.client("ec2",region_name="ap-south-1")
    instance,name = validate_instance(ec2,iid,os.environ["ALLOWED_INSTANCE_IDS"].split(","))
    state = instance["State"]["Name"]
    if args.action == "validate":
        if state != "stopped":
            raise ValueError(f"Expected stopped instance; found {state}. No recovery started.")
        with open(os.environ["GITHUB_OUTPUT"],"a") as f:
            f.write("instance_name="+name+"\n")
    elif args.action == "start":
        if state != "stopped":
            raise ValueError(f"Expected stopped instance; found {state}")
        ec2.start_instances(InstanceIds=[iid])
        print(f"StartInstances accepted for {iid}")
    else:
        ec2.get_waiter("instance_running").wait(InstanceIds=[iid],WaiterConfig={"Delay":15,"MaxAttempts":40})
        ec2.get_waiter("instance_status_ok").wait(InstanceIds=[iid],WaiterConfig={"Delay":15,"MaxAttempts":60})
        elb = boto3.client("elbv2",region_name="ap-south-1")
        deadline = time.monotonic()+600
        while time.monotonic()<deadline:
            health = elb.describe_target_health(TargetGroupArn=os.environ["TARGET_GROUP_ARN"],Targets=[{"Id":iid,"Port":80}])
            statuses = health["TargetHealthDescriptions"]
            if statuses and all(x["TargetHealth"]["State"] == "healthy" for x in statuses):
                print(f"EC2 checks and Apache ALB health passed: {iid}")
                return
            time.sleep(15)
        raise TimeoutError("EC2 started but ALB target did not become healthy")
if __name__ == "__main__":
    main()
