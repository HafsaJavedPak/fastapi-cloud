# FastAPI AWS Free-Tier Deployment — Master Plan (Part 2 of 2)
## Pipeline · Configuration · Full Test Matrix

---

## 7. Implementation Phases (Execution Order)

| Phase | Step | What Gets Built | Verify Before Proceeding |
|-------|------|-----------------|--------------------------|
| **P1** | 1 | FastAPI app code | `pytest tests/ -v` all green |
| **P1** | 2 | Dockerfile | `docker build` + `docker run` + curl `/health` |
| **P1** | 3 | requirements.txt | pip install clean |
| **P2** | 4 | ECR repository | `aws ecr describe-repositories` |
| **P2** | 5 | GitHub Actions workflow | Dry-run push to feature branch |
| **P3** | 6 | VPC / Security Groups | `aws ec2 describe-security-groups` |
| **P3** | 7 | IAM roles + instance profiles | `aws iam simulate-principal-policy` |
| **P3** | 8 | Launch Template | `aws ec2 describe-launch-templates` |
| **P3** | 9 | Auto Scaling Group | Instance appears in EC2 console |
| **P4** | 10 | Nginx proxy EC2 | `curl http://<EIP>/health` returns 200 |
| **P4** | 11 | Lambda + EventBridge | Invoke Lambda manually, check Nginx upstream |
| **P5** | 12 | ACM certificate | Status = Issued |
| **P5** | 13 | CloudFront distribution | `curl https://<cf-domain>/health` returns 200 |
| **P6** | 14 | End-to-end smoke test | All 5 endpoints via CloudFront HTTPS |

---

## 8. GitHub Actions Workflow — Full Spec

**File:** `.github/workflows/deploy.yml`

```yaml
name: CI/CD → ECR → (Trigger ASG refresh)

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  AWS_REGION: us-east-1
  ECR_REPOSITORY: fastapi-demo
  IMAGE_TAG: ${{ github.sha }}

jobs:
  test:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: pytest tests/ -v --tb=short

  build-push:
    name: Build & Push to ECR
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials (OIDC — no long-lived keys)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT_ID:role/github-actions-ecr-role
          aws-region: ${{ env.AWS_REGION }}

      - name: Login to ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build, tag, push
        run: |
          IMAGE_URI=${{ steps.login-ecr.outputs.registry }}/${{ env.ECR_REPOSITORY }}
          docker build -t $IMAGE_URI:${{ env.IMAGE_TAG }} -t $IMAGE_URI:latest .
          docker push $IMAGE_URI:${{ env.IMAGE_TAG }}
          docker push $IMAGE_URI:latest

      - name: Trigger ASG Instance Refresh
        run: |
          aws autoscaling start-instance-refresh \
            --auto-scaling-group-name fastapi-demo-asg \
            --preferences '{"MinHealthyPercentage":50,"InstanceWarmup":60}'
```

> **OIDC vs IAM User:** Using OIDC (`role-to-assume`) means **zero long-lived AWS credentials** stored in GitHub Secrets — just the role ARN. This is the security best practice.

### GitHub Actions IAM Role (OIDC)
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:YOUR_ORG/YOUR_REPO:ref:refs/heads/main"
      }
    }
  }]
}
```

---

## 9. EC2 User Data Script (Launch Template)

```bash
#!/bin/bash
set -e
# Install Docker
yum update -y
yum install -y docker
systemctl enable docker
systemctl start docker

# Install SSM agent (already present on Amazon Linux 2023)
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

# Pull and run container
REGION="us-east-1"
ACCOUNT_ID="ACCOUNT_ID"
REPO="fastapi-demo"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:latest"

# Auth to ECR (instance role provides credentials)
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin \
    ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

# Set instance ID for app logging
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)

# Run the container
docker run -d \
  --name fastapi-app \
  --restart always \
  -p 8000:8000 \
  -e INSTANCE_ID=$INSTANCE_ID \
  $IMAGE_URI
```

---

## 10. Nginx Configuration

### `/etc/nginx/nginx.conf`
```nginx
user www-data;
worker_processes auto;
pid /run/nginx.pid;

events { worker_connections 1024; }

http {
    sendfile on;
    tcp_nopush on;
    types_hash_max_size 2048;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    # Real IP from CloudFront
    set_real_ip_from 0.0.0.0/0;
    real_ip_header X-Forwarded-For;

    log_format main '$remote_addr - $host [$time_local] '
                    '"$request" $status $body_bytes_sent '
                    '"$http_referer" "$http_user_agent"';
    access_log /var/log/nginx/access.log main;
    error_log  /var/log/nginx/error.log warn;

    include /etc/nginx/conf.d/*.conf;
}
```

### `/etc/nginx/conf.d/app.conf`
```nginx
# Upstream defined dynamically — rewritten by Lambda via SSM
include /etc/nginx/conf.d/upstream.conf;

server {
    listen 80;
    server_name _;

    location /health {
        access_log off;
        return 200 '{"status":"nginx-ok"}';
        add_header Content-Type application/json;
    }

    location / {
        proxy_pass         http://fastapi_backend;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_connect_timeout  5s;
        proxy_read_timeout    60s;
        proxy_next_upstream    error timeout http_502 http_503;
    }
}
```

### `/etc/nginx/conf.d/upstream.conf` (initial)
```nginx
upstream fastapi_backend {
    server 127.0.0.1:8000;  # placeholder; rewritten by Lambda on first ASG event
}
```

---

## 11. Lambda Function — Nginx Sync

**File:** `lambda/nginx_sync.py`

```python
import boto3, json, os, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

ASG_NAME         = os.environ["ASG_NAME"]           # fastapi-demo-asg
PROXY_INSTANCE   = os.environ["PROXY_INSTANCE_ID"]  # i-0abc123...
UPSTREAM_FILE    = "/etc/nginx/conf.d/upstream.conf"

def handler(event, context):
    logger.info("Event: %s", json.dumps(event))

    asg   = boto3.client("autoscaling")
    ec2   = boto3.client("ec2")
    ssm   = boto3.client("ssm")

    # 1. Get healthy instance IDs from ASG
    resp  = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
    group = resp["AutoScalingGroups"][0]

    instance_ids = [
        i["InstanceId"]
        for i in group["Instances"]
        if i["LifecycleState"] == "InService"
        and i["HealthStatus"] == "Healthy"
    ]
    logger.info("Healthy instances: %s", instance_ids)

    if not instance_ids:
        logger.warning("No healthy instances — keeping current upstream")
        return {"status": "no_change"}

    # 2. Resolve private IPs
    reservations = ec2.describe_instances(InstanceIds=instance_ids)["Reservations"]
    ips = [
        inst["PrivateIpAddress"]
        for r in reservations
        for inst in r["Instances"]
    ]
    logger.info("Private IPs: %s", ips)

    # 3. Build upstream block
    server_lines = "\n".join(f"    server {ip}:8000;" for ip in ips)
    upstream_conf = f"""upstream fastapi_backend {{
{server_lines}
    keepalive 32;
}}
"""

    # 4. Write file + reload via SSM
    shell_cmd = (
        f"cat > {UPSTREAM_FILE} << 'EOF'\n{upstream_conf}\nEOF\n"
        f"nginx -t && nginx -s reload"
    )
    resp = ssm.send_command(
        InstanceIds=[PROXY_INSTANCE],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [shell_cmd]},
        TimeoutSeconds=30,
    )
    cmd_id = resp["Command"]["CommandId"]
    logger.info("SSM command sent: %s", cmd_id)
    return {"status": "updated", "instances": ips, "ssm_command": cmd_id}
```

---

## 12. Complete Test Matrix

### 12.1 Component Tests (run immediately after each component is created)

#### A. FastAPI Application
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| APP-01 | `GET /` returns 200 + `status:healthy` | pytest / curl | HTTP 200 |
| APP-02 | `GET /health` returns 200 + checks map | pytest | HTTP 200 |
| APP-03 | `POST /items` creates item, returns 201 + UUID | pytest | HTTP 201, UUID in body |
| APP-04 | `POST /items` missing price → 422 | pytest | HTTP 422 |
| APP-05 | `GET /items` empty store → `[]` | pytest | HTTP 200, body=`[]` |
| APP-06 | `GET /items/{id}` returns correct item | pytest | HTTP 200, correct fields |
| APP-07 | `GET /items/ghost` → 404 | pytest | HTTP 404 |
| APP-08 | `PUT /items/{id}` full replace | pytest | HTTP 200, all fields updated |
| APP-09 | `PUT /items/{id}` preserves `created_at` | pytest | timestamps match |
| APP-10 | `PATCH /items/{id}` only updates supplied fields | pytest | unchanged fields intact |
| APP-11 | `DELETE /items/{id}` → 204 | pytest | HTTP 204 |
| APP-12 | `DELETE` then `GET` same id → 404 | pytest | HTTP 404 |
| APP-13 | `POST` negative price → 422 | pytest | HTTP 422 |
| APP-14 | `POST` negative stock → 422 | pytest | HTTP 422 |

**Run:** `pytest tests/ -v --tb=short`

#### B. Docker Image
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| DOC-01 | Image builds without error | `docker build` | Exit 0 |
| DOC-02 | Container starts on port 8000 | `docker run` | `docker ps` shows healthy |
| DOC-03 | `GET /health` via curl to container | `curl localhost:8000/health` | HTTP 200 |
| DOC-04 | Container runs as non-root | `docker exec whoami` | Not `root` |
| DOC-05 | Image size reasonable (< 200 MB) | `docker images` | Size < 200 MB |
| DOC-06 | `INSTANCE_ID` env var passed through | `curl /` | Shows env var value |

#### C. ECR Repository
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| ECR-01 | Repository exists | `aws ecr describe-repositories` | Returns repo |
| ECR-02 | Push image succeeds | `docker push` | Exit 0 |
| ECR-03 | Image visible in registry | `aws ecr list-images` | SHA tag present |
| ECR-04 | Lifecycle policy applied | `aws ecr get-lifecycle-policy` | Policy JSON returned |
| ECR-05 | Pull from EC2 via instance role | `docker pull` on EC2 | Exit 0, no manual creds |

#### D. GitHub Actions Pipeline
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| GHA-01 | Workflow triggers on push to main | git push | Workflow starts in Actions tab |
| GHA-02 | Test job runs pytest | GitHub Actions UI | All tests green |
| GHA-03 | Build job runs after tests pass | GitHub Actions UI | Job chain completes |
| GHA-04 | Image appears in ECR after push | `aws ecr list-images` | New SHA tag present |
| GHA-05 | PR does NOT push to ECR | git push to feature branch | Only test job runs |
| GHA-06 | Failed test blocks build | Introduce a breaking test | Build job does NOT run |

#### E. Security Groups
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| SG-01 | App instance port 8000 not reachable from public | `curl http://<app-public-ip>:8000` | Connection refused / timeout |
| SG-02 | App instance reachable from Nginx proxy | `curl http://<app-private-ip>:8000` from proxy | HTTP 200 |
| SG-03 | Nginx port 80 reachable from CloudFront | CloudFront fetch | 200 (after CF setup) |
| SG-04 | Nginx port 80 blocked from arbitrary IP | `curl http://<EIP>` from your laptop | Timeout (blocked by SG) |

#### F. Auto Scaling Group
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| ASG-01 | 1 instance launches after ASG creation | EC2 console / `aws ec2 describe-instances` | 1 running instance |
| ASG-02 | Instance has correct tags | `aws ec2 describe-instances --filters` | Tags match |
| ASG-03 | Container running on instance | `ssm start-session` → `docker ps` | Container up |
| ASG-04 | App responds on port 8000 | From Nginx proxy: `curl http://<private-ip>:8000/health` | HTTP 200 |
| ASG-05 | Scale-out triggers on CPU spike | `stress` command via SSM | 2nd instance appears within 3 min |
| ASG-06 | Lambda triggered on scale-out | CloudWatch Logs for Lambda | Log shows new IP added |
| ASG-07 | Nginx upstream updated after scale-out | `cat /etc/nginx/conf.d/upstream.conf` | 2 server lines |
| ASG-08 | Scale-in removes instance | CPU drops → wait cooldown | Instance terminated |
| ASG-09 | Lambda triggered on scale-in | CloudWatch Logs | Log shows IP removed |

#### G. Nginx Proxy
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| NGX-01 | Nginx service running | `systemctl status nginx` | Active (running) |
| NGX-02 | `GET /health` from Nginx itself | `curl http://localhost/health` | `{"status":"nginx-ok"}` |
| NGX-03 | Proxy forwards to app | `curl http://<EIP>/items` (from whitelisted IP) | HTTP 200 |
| NGX-04 | upstream.conf syntax valid | `nginx -t` | `syntax is ok` |
| NGX-05 | `nginx -s reload` after upstream change | Manual rewrite + reload | No downtime, new IPs used |
| NGX-06 | `proxy_next_upstream` skips dead server | Stop one app container | Requests succeed on other |

#### H. EventBridge + Lambda + SSM
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| EVT-01 | EventBridge rule created and enabled | `aws events list-rules` | Rule in ENABLED state |
| EVT-02 | Lambda function exists + correct role | `aws lambda get-function` | Returns config |
| EVT-03 | Lambda invokes manually | `aws lambda invoke --payload '{}'` | Status 200, no errors |
| EVT-04 | Lambda writes to CloudWatch Logs | CloudWatch console | Log stream with IPs |
| EVT-05 | SSM command sent by Lambda | `aws ssm list-command-invocations` | Command succeeded |
| EVT-06 | upstream.conf updated post-Lambda | `cat /etc/nginx/conf.d/upstream.conf` on proxy | Correct IPs |
| EVT-07 | Full auto-trigger: launch ASG instance | Manual instance launch | Within 2 min, upstream updated |

#### I. ACM Certificate
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| ACM-01 | Certificate requested in us-east-1 | `aws acm list-certificates --region us-east-1` | Cert listed |
| ACM-02 | Certificate status = ISSUED | Console / CLI | Status: ISSUED |
| ACM-03 | Domain validation CNAME set | DNS lookup | CNAME resolves |

#### J. CloudFront Distribution
| Test ID | What to Test | Tool | Pass Condition |
|---------|-------------|------|----------------|
| CF-01 | Distribution deployed (not In Progress) | Console / `aws cloudfront list-distributions` | Status: Deployed |
| CF-02 | HTTPS `GET /` returns 200 | `curl https://<cf-domain>/` | HTTP 200 |
| CF-03 | HTTP redirects to HTTPS | `curl -I http://<cf-domain>/` | 301/302 to HTTPS |
| CF-04 | Valid SSL cert (no warnings) | Browser / `openssl s_client` | No cert errors |
| CF-05 | `X-Cache` header present | `curl -I https://<cf-domain>/health` | `X-Cache: Miss/Hit` |
| CF-06 | Origin IP hidden in response headers | `curl -I https://<cf-domain>/` | No `Server` IP leak |

---

### 12.2 End-to-End Architecture Tests (run after all components deployed)

| Test ID | Scenario | Steps | Pass Condition |
|---------|----------|-------|----------------|
| E2E-01 | Full CRUD cycle via CloudFront | POST→GET→PUT→PATCH→DELETE via `https://<cf-domain>` | All return correct HTTP codes |
| E2E-02 | HTTPS enforced | `curl http://<cf-domain>/items` | 301 redirect |
| E2E-03 | Scaling event → zero downtime | Trigger scale-out, keep curling `/health` every second | No 5xx errors during scale event |
| E2E-04 | New deployment via git push | Push code change → watch Actions → verify new container running | Change visible via API within 5 min |
| E2E-05 | Instance replacement | Terminate app instance manually | ASG replaces it; Lambda updates Nginx; traffic recovers |
| E2E-06 | Load distribution | With 2 instances, check `instance_id` in `GET /` | Both instance IDs appear in responses |
| E2E-07 | CloudFront caching disabled for API | POST item, immediately GET — fresh data | No stale cache responses |
| E2E-08 | Security: direct EC2 access blocked | `curl http://<app-public-ip>:8000` | Timeout / refused |
| E2E-09 | Security: direct Nginx access blocked | `curl http://<EIP>` from non-CF IP | Timeout / refused |
| E2E-10 | Docker image updated on deploy | Push new image, trigger refresh | `GET /` shows new version |

---

### 12.3 Load / Stress Test (Optional — Free Tier Safe)

```bash
# Install hey (HTTP load generator)
go install github.com/rakyll/hey@latest

# 50 concurrent, 500 total requests via CloudFront
hey -n 500 -c 50 https://<cf-domain>/health

# Watch CPU on app instance — should trigger scale-out at 15%
# use 'stress' via SSM for controlled CPU spike:
aws ssm send-command \
  --instance-ids i-0abc123 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["stress --cpu 1 --timeout 180s"]}'
```

---

## 13. Rollback Strategy

| Scenario | Rollback Action | Time to Recover |
|----------|----------------|-----------------|
| Bad Docker image | ECR: re-tag previous SHA as `latest`; trigger ASG refresh | ~3 min |
| Bad ASG launch template | Update launch template to previous version | ~2 min |
| Lambda bug breaks upstream | Manually set `upstream.conf` via SSM, reload Nginx | ~1 min |
| CloudFront misconfiguration | Update distribution origin or behaviour | ~5–15 min propagation |

---

## 14. Monitoring (Free Tier)

| Signal | Source | Action |
|--------|--------|--------|
| App instance CPU | CloudWatch (included free) | Alert > 70% sustained |
| ASG instance count | CloudWatch | Alert if drops to 0 |
| Lambda errors | CloudWatch Logs | Alert on any ERROR line |
| Nginx 5xx rate | CloudWatch Logs Metric Filter | Alert > 5 errors/min |
| CloudFront 5xx | CloudFront standard metrics (free) | Alert > 1% error rate |

All CloudWatch alarms → SNS → Email (SNS free tier: 1 000 email notifications/month).

---

## 15. File Structure (Final Repository Layout)

```
fastapi-cloud/
├── .github/
│   └── workflows/
│       └── deploy.yml          # CI/CD pipeline
├── app/
│   ├── __init__.py
│   └── main.py                 # FastAPI app (5 endpoints)
├── lambda/
│   └── nginx_sync.py           # EventBridge → Nginx upstream sync
├── nginx/
│   ├── nginx.conf              # Main Nginx config
│   ├── app.conf                # Server block
│   └── upstream.conf           # Dynamic upstream (managed by Lambda)
├── scripts/
│   ├── setup_nginx_proxy.sh    # One-time Nginx EC2 bootstrap
│   └── userdata_app.sh         # ASG instance user data
├── tests/
│   ├── __init__.py
│   └── test_main.py            # All pytest tests (14 test cases)
├── Dockerfile                  # Multi-stage build
├── requirements.txt            # Pinned deps
├── .gitignore
└── README.md
```
