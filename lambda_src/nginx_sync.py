"""
Descrption of file:
This code is an AWS Lambda function designed to act as a dynamic service discovery tool for an Nginx reverse proxy.
When you use an AWS Auto Scaling Group (ASG), backend servers (like your FastAPI app) spin up and shut down automatically based on traffic. Nginx needs to know the exact IP addresses of these servers to route traffic to them. This script automatically finds all currently healthy instances in the ASG, optionally tests if their API port is actually responding, generates a new Nginx configuration file, and uses AWS Systems Manager (SSM) to inject that file into your Nginx server and reload it.
"""
import json  # Imports the json module for parsing and formatting JSON data (used here for logging the event)
import logging  # Imports the logging module to record what the script is doing for troubleshooting in AWS CloudWatch
import os  # Imports the os module to read environment variables passed to the Lambda function
import socket  # Imports the socket module to perform low-level network connection tests (TCP checks)

import boto3  # Imports boto3, the official AWS SDK for Python, allowing the script to interact with AWS services

logger = logging.getLogger()  # Creates a logger object to handle outputting log messages
logger.setLevel(logging.INFO)  # Sets the logging level to INFO, meaning it will record general events, warnings, and errors

ASG_NAME = os.environ["ASG_NAME"]  # Retrieves the name of the Auto Scaling Group from the Lambda's environment variables
PROXY_INSTANCE = os.environ["PROXY_INSTANCE_ID"]  # Retrieves the EC2 Instance ID of the Nginx server from environment variables
UPSTREAM_FILE = "/etc/nginx/conf.d/upstream.conf"  # Defines the file path on the Nginx server where the backend IPs will be saved
APP_PORT = 8000  # Sets the network port that the backend FastAPI application is listening on
PROBE_TIMEOUT = 2  # Sets a 2-second timeout limit for the TCP network check to prevent the script from hanging


def _is_ready(ip: str) -> bool:  # Defines a helper function that takes an IP address and returns a True/False boolean
    """TCP connect check — Lambda is in AWS network, can reach instance private IPs
    only if VPC-attached. If not VPC-attached, this returns False for all; see note."""  # Docstring explaining network requirements
    try:  # Starts a block to catch potential network connection errors
        with socket.create_connection((ip, APP_PORT), timeout=PROBE_TIMEOUT):  # Attempts to open a TCP connection to the IP on port 8000
            return True  # If the connection succeeds without throwing an error, the server is ready, so return True
    except OSError:  # Catches any operating system or network errors (like Connection Refused or Timeout)
        return False  # If it fails, the server isn't ready to receive traffic yet, so return False


def _build_conf(ips):  # Defines a helper function that takes a list of ready IP addresses and generates the Nginx config
    lines = "\n".join(f"    server {ip}:{APP_PORT} max_fails=2 fail_timeout=10s;" for ip in ips)  # Creates an Nginx 'server' line for each IP and joins them with line breaks
    return f"upstream fastapi_backend {{\n{lines}\n    keepalive 32;\n}}\n"  # Wraps those server lines inside an Nginx 'upstream' block and returns the complete text


def handler(event, context):  # Defines the main entry point function that AWS Lambda automatically runs when triggered
    logger.info("Event: %s", json.dumps(event))  # Logs the raw event data that triggered the Lambda (useful for debugging)

    asg = boto3.client("autoscaling")  # Initializes a boto3 client to talk to the AWS Auto Scaling service
    ec2 = boto3.client("ec2")  # Initializes a boto3 client to talk to the AWS EC2 (virtual machine) service
    ssm = boto3.client("ssm")  # Initializes a boto3 client to talk to AWS Systems Manager (used to run commands on servers)

    group = asg.describe_auto_scaling_groups(  # Asks AWS for details about specific Auto Scaling Groups
        AutoScalingGroupNames=[ASG_NAME]  # Tells AWS to only look up the specific ASG name we stored in the environment variables
    )["AutoScalingGroups"][0]  # Extracts the first (and only) group from the resulting AWS response list

    instance_ids = [  # Starts a list comprehension to extract a list of specific instance IDs
        i["InstanceId"]  # Plucks the "InstanceId" string from the dictionary
        for i in group["Instances"]  # Loops through every instance currently attached to the Auto Scaling Group
        if i["LifecycleState"] == "InService" and i["HealthStatus"] == "Healthy"  # Filters out instances that are still booting up, shutting down, or marked unhealthy by AWS
    ]  # Closes the list comprehension
    logger.info("InService instances: %s", instance_ids)  # Logs the list of instance IDs that AWS considers healthy

    if not instance_ids:  # Checks if the list of healthy instance IDs is empty (meaning all servers are down or booting)
        logger.warning("No healthy instances — keeping current upstream")  # Logs a warning that no servers were found
        return {"status": "no_change", "reason": "no_instances"}  # Exits the Lambda early without changing Nginx so we don't accidentally wipe out the routing table entirely

    reservations = ec2.describe_instances(InstanceIds=instance_ids)["Reservations"]  # Asks EC2 for full networking details of the healthy instances we just found
    ips = [  # Starts a list comprehension to extract Private IP addresses
        inst["PrivateIpAddress"]  # Plucks out the internal, private IP address of the instance
        for r in reservations  # Loops through the "Reservations" (AWS grouping for instances launched together)
        for inst in r["Instances"]  # Loops through the individual instances within each reservation
        if inst.get("PrivateIpAddress")  # Ensures we only grab it if the instance actually has a private IP assigned
    ]  # Closes the list comprehension
    logger.info("Candidate IPs: %s", ips)  # Logs the raw list of IP addresses we just retrieved

    ready = [ip for ip in ips if _is_ready(ip)] if os.getenv("PROBE_ENABLED") == "1" else ips  # If 'PROBE_ENABLED' is 1, runs the TCP check on all IPs to filter out ones not yet listening; otherwise just uses all IPs
    logger.info("Ready IPs: %s", ready)  # Logs the final list of IPs that are confirmed to be ready for traffic

    if not ready:  # Checks if our probe filtered out all the IPs (meaning none are actually listening on port 8000 yet)
        logger.warning("No ready backends — keeping current upstream")  # Logs a warning
        return {"status": "no_change", "reason": "none_ready"}  # Exits the Lambda early to avoid breaking Nginx with an empty config

    upstream_conf = _build_conf(ready)  # Passes the final list of ready IPs to our helper function to generate the raw Nginx config text
    shell_cmd = (  # Starts defining a bash shell command that will be executed on the Nginx server
        f"cat > {UPSTREAM_FILE} << 'EOF'\n{upstream_conf}EOF\n"  # Uses 'cat' and a Heredoc ('EOF') to overwrite the Nginx config file with our newly generated text
        f"nginx -t && nginx -s reload"  # Tests the new Nginx config for syntax errors, and if it passes (&&), reloads Nginx gracefully without dropping connections
    )  # Closes the bash command string

    resp = ssm.send_command(  # Uses AWS Systems Manager to remotely execute our bash command on the Nginx proxy server
        InstanceIds=[PROXY_INSTANCE],  # Targets the specific Nginx EC2 instance ID we defined in the environment variables
        DocumentName="AWS-RunShellScript",  # Tells SSM we want to run a raw shell script
        Parameters={"commands": [shell_cmd]},  # Passes our generated bash script into the command parameters
        TimeoutSeconds=60,  # Tells SSM to time out and fail if the command takes longer than 60 seconds to execute
    )  # Closes the SSM command request
    cmd_id = resp["Command"]["CommandId"]  # Extracts the unique tracking ID for the remote command we just sent
    logger.info("SSM command sent: %s", cmd_id)  # Logs the tracking ID so you can look up if the script succeeded inside the AWS Console
    return {"status": "updated", "instances": ready, "ssm_command": cmd_id}  # Exits the Lambda function, returning a JSON summary of what was done
