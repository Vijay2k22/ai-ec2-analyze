import argparse
import os
import smtplib
import ssl
from email.message import EmailMessage

p=argparse.ArgumentParser()
p.add_argument("status",choices=["shutdown","starting","success","failure"])
a=p.parse_args()
titles={"shutdown":"EC2 Shutdown Detected","starting":"EC2 Start Initiated","success":"EC2 Started Successfully","failure":"EC2 Recovery Failed"}
msg=EmailMessage()
msg["Subject"]=f"[{titles[a.status]}] {os.environ.get('INSTANCE_NAME') or os.environ['INSTANCE_ID']}"
msg["From"]=os.environ["SMTP_USER"]
msg["To"]=os.environ["ALERT_EMAIL"]
msg.set_content(f"""Status: {titles[a.status]}
Instance name: {os.environ.get('INSTANCE_NAME','Unknown')}
Instance ID: {os.environ['INSTANCE_ID']}
Region: ap-south-1 (Mumbai)
Detected at (UTC): {os.environ.get('DETECTED_AT','Manual run')}
Pipeline: {os.environ['RUN_URL']}

{os.environ.get('DETAIL','Open the pipeline link for stage details.')}
""")
with smtplib.SMTP(os.environ["SMTP_HOST"],int(os.environ.get("SMTP_PORT","587")),timeout=30) as smtp:
    smtp.ehlo()
    smtp.starttls(context=ssl.create_default_context())
    smtp.ehlo()
    smtp.login(os.environ["SMTP_USER"],os.environ["SMTP_PASSWORD"])
    smtp.send_message(msg)
print("Email accepted by SMTP server:",titles[a.status])
