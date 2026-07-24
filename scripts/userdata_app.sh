#!/bin/bash

# ---------------------------------------------------------
# 1. STRICT ERROR HANDLING & LOGGING
# ---------------------------------------------------------
# -e: Immediately exit the script if any command fails (prevents silent failures)
# -u: Exit if the script tries to use an undefined variable
# -x: Print every command to the console before running it (great for debugging)
# -o pipefail: Catch errors that happen inside piped commands (like grep or tee)
set -euxo pipefail

# Capture all standard output and error output, and save it to two places:
# 1. A physical file on the server: /var/log/user-data.log
# 2. The system logger, tagged as "user-data"
exec > >(tee /var/log/user-data.log|logger -t user-data) 2>&1

# ---------------------------------------------------------
# 2. INSTALL DEPENDENCIES
# ---------------------------------------------------------
# Update all installed packages to their latest secure versions
dnf update -y

# Install Docker
dnf install -y docker

# Start the Docker service immediately (--now) and ensure it starts on future reboots (--enable)
systemctl enable --now docker

# Ensure the SSM Agent is running so your Lambda function can connect to this server
systemctl enable --now amazon-ssm-agent

# ---------------------------------------------------------
# 3. CONFIGURE ECR PUBLIC ENVIRONMENT VARIABLES
# ---------------------------------------------------------
# NOTE: Replace 'YOUR_PUBLIC_ALIAS' with your actual ECR Public alias (e.g., x7w8b8b7)
IMAGE_URI="public.ecr.aws/x7w8b8b7/fastapi-demo:latest"

# ---------------------------------------------------------
# 4. AUTHENTICATE WITH AWS ECR PUBLIC
# ---------------------------------------------------------
# Request a temporary Docker login password from ECR Public and pipe it directly to Docker
aws ecr-public get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin public.ecr.aws

# ---------------------------------------------------------
# 5. FETCH SERVER METADATA (IMDSv2)
# ---------------------------------------------------------
# Securely request a temporary session token from the EC2 metadata service
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 300")

# Use that token to ask AWS: "What is my own Instance ID?" (e.g., i-0abcd12345efg)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

# ---------------------------------------------------------
# 6. LAUNCH THE APPLICATION
# ---------------------------------------------------------
# Run the Docker container:
# -d: Run in the background (detached)
# --name: Call the container "fastapi-app"
# --restart always: If the app crashes, automatically restart it
# -p 8000:8000: Map port 8000 on the EC2 server to port 8000 inside the container
# -e INSTANCE_ID=$INSTANCE_ID: Pass the server's ID into the Python app as an environment variable
docker run -d --name fastapi-app --restart always -p 8000:8000 \
  -e INSTANCE_ID=$INSTANCE_ID $IMAGE_URI
