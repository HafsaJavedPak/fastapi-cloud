# FastAPI AWS Free-Tier Deployment — Master Plan (Part 1 of 2)
## Architecture · IAM Policies · Free-Tier Cost Guard-rails

---

## 1. Architecture Overview

```
Developer Laptop
      │  git push
      ▼
┌─────────────────────────────────────────────────────────┐
│  GitHub Repository  +  GitHub Actions (free runners)    │
│  ① pytest → ② docker build → ③ docker push → ECR       │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼ (image tag = git SHA)
                   Amazon ECR  (500 MB free/mo)
                          │
          ┌───────────────┘ (Launch Template pulls on boot)
          ▼
┌──────────────────────────────────────────────────┐
│  Auto Scaling Group  (min=1, max=2)              │
│  t2.micro / t3.micro  ─ port 8000               │
│  Scaling: CPU target-tracking  ~15–20 %          │
│  SG: inbound 8000  ONLY from Nginx-Proxy SG      │
└──────────────────────────────────────────────────┘
          │  ASG lifecycle events
          ▼
┌──────────────────────────────────────────────────┐
│  EventBridge Rule  (EC2 Launch / Terminate)      │
│  → Lambda  (queries ASG private IPs)             │
│  → SSM Run Command  (rewrites upstream.conf)     │
│  → nginx -s reload  (zero-drop)                  │
└──────────────────────────────────────────────────┘
          │  forwards traffic
          ▼
┌──────────────────────────────────────────────────┐
│  Nginx Proxy EC2  (t2.micro + Elastic IP)        │
│  port 80 / 443  ← CloudFront IP ranges only      │
│  dynamic upstream → ASG instances :8000          │
└──────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────┐
│  AWS CloudFront Distribution                     │
│  Origin = Elastic IP of Nginx Proxy              │
│  ACM cert (us-east-1)  HTTPS termination         │
│  Free: 1 TB egress + 10 M req/mo                 │
└──────────────────────────────────────────────────┘
          │  https://<cloudfront-domain>
          ▼
       Client / Browser
```

---

## 2. Component Breakdown & Decisions

### 2.1 GitHub + GitHub Actions
| Detail | Value |
|--------|-------|
| Trigger | `push` to `main` branch |
| Runner | `ubuntu-latest` (free 2 000 min/mo) |
| Steps | lint → pytest → docker build → ECR push |
| Image tag | `$GITHUB_SHA` (short SHA) + `latest` |
| Secrets stored in | GitHub Actions Secrets (not in code) |

### 2.2 Amazon ECR
| Detail | Value |
|--------|-------|
| Registry type | Private |
| Repository name | `fastapi-demo` |
| Lifecycle policy | Keep last 5 images → delete older (saves storage) |
| Free tier | 500 MB/month storage |
| Auth | `aws-actions/amazon-ecr-login@v2` action |

### 2.3 Auto Scaling Group (ASG)
| Detail | Value |
|--------|-------|
| Instance type | `t2.micro` (free tier eligible) |
| AMI | Latest Amazon Linux 2023 |
| Min / Max | 1 / 2 |
| Desired | 1 |
| Scaling metric | Average CPU Utilization |
| Target value | 15 % (triggers fast for demo; raise to 60 % in prod) |
| Cooldown | 120 s scale-out / 300 s scale-in |
| User Data | pulls ECR image, runs `docker run -d -p 8000:8000` |
| Health check | EC2 type (HTTP ALB not used → saves cost) |

### 2.4 Nginx Proxy EC2
| Detail | Value |
|--------|-------|
| Instance type | `t2.micro` |
| OS | Ubuntu 22.04 LTS |
| Elastic IP | 1 (free while attached) |
| Nginx version | Latest stable (apt) |
| Dynamic config | `/etc/nginx/conf.d/upstream.conf` rewritten by SSM |
| Reload | `nginx -s reload` (no downtime) |
| Health check | `proxy_next_upstream error timeout` → skips dead upstreams |

### 2.5 EventBridge + Lambda + SSM
| Detail | Value |
|--------|-------|
| EventBridge rule | `EC2 Instance Launch Successful` + `EC2 Instance Terminate Successful` |
| Lambda runtime | Python 3.12 |
| Lambda timeout | 30 s |
| Lambda memory | 128 MB (minimum = cheapest) |
| Lambda action | `describe_auto_scaling_groups` → build upstream list → SSM `SendCommand` |
| SSM document | `AWS-RunShellScript` |
| Free tier | Lambda 1 M req/mo; SSM commands free at this scale |

### 2.6 ACM + CloudFront
| Detail | Value |
|--------|-------|
| ACM region | `us-east-1` (required for CloudFront) |
| Cert type | Public (DNS validated via Route 53 or manual CNAME) |
| CloudFront origin | `http://<Elastic-IP>` (port 80) |
| Viewer protocol | Redirect HTTP → HTTPS |
| Cache behavior | `CachingDisabled` for API (TTL=0) |
| Free tier | 1 TB egress + 10 M requests/month |

---

## 3. Free-Tier Cost Analysis

| Service | Free Tier Allowance | Expected Usage | Buffer |
|---------|---------------------|---------------|--------|
| EC2 (t2.micro) | 750 h/mo combined | ~1 460 h (2 instances) — **NOTE** | Use 1 instance normally |
| ECR | 500 MB/mo | ~150 MB (5 images × ~30 MB) | ✅ Safe |
| Lambda | 1 M req + 400 K GB-s | < 100 invocations/mo | ✅ Safe |
| CloudFront | 1 TB + 10 M req | Demo traffic < 1 GB | ✅ Safe |
| Elastic IP | $0 while attached | 1 EIP attached 24/7 | ✅ Safe |
| SSM | Free for standard usage | < 100 commands/mo | ✅ Safe |
| EventBridge | 1 M events/mo free | < 100 events/mo | ✅ Safe |
| Data transfer | 100 GB/mo free | Demo << 100 GB | ✅ Safe |

> **⚠ EC2 Hour Budget Warning:**  
> Free tier = 750 combined hours/month. Running 2 t2.micro instances 24/7 = 1 460 hours.  
> **Strategy:** Primary app instance runs 24/7 (730 h). Nginx proxy runs 24/7 (730 h).  
> Total = 1 460 h — **exceeds free tier by ~710 hours** (~$7/mo at $0.0116/hr).  
> **Mitigation:** Stop Nginx proxy when not demoing OR use one instance as combined app+proxy (not recommended for security). For the 12-month free tier window this is $0–$84 total — acceptable for a demo.

---

## 4. IAM Roles & Policies

### 4.1 GitHub Actions IAM User
**Name:** `github-actions-deployer`  
**Purpose:** Push images to ECR, read/write for deployment

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "ECRImagePush",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:DescribeRepositories",
        "ecr:ListImages"
      ],
      "Resource": "arn:aws:ecr:REGION:ACCOUNT_ID:repository/fastapi-demo"
    }
  ]
}
```

### 4.2 EC2 App Instance Role
**Name:** `fastapi-app-instance-role`  
**Attached to:** Launch Template → Instance Profile

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRPull",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SSMCoreAgent",
      "Effect": "Allow",
      "Action": [
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
        "ec2messages:AcknowledgeMessage",
        "ec2messages:DeleteMessage",
        "ec2messages:FailMessage",
        "ec2messages:GetEndpoint",
        "ec2messages:GetMessages",
        "ec2messages:SendReply"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:REGION:ACCOUNT_ID:log-group:/fastapi-demo/*"
    }
  ]
}
```

### 4.3 Nginx Proxy Instance Role
**Name:** `nginx-proxy-instance-role`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SSMCoreAgent",
      "Effect": "Allow",
      "Action": [
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
        "ec2messages:AcknowledgeMessage",
        "ec2messages:DeleteMessage",
        "ec2messages:FailMessage",
        "ec2messages:GetEndpoint",
        "ec2messages:GetMessages",
        "ec2messages:SendReply"
      ],
      "Resource": "*"
    }
  ]
}
```

### 4.4 Lambda Execution Role
**Name:** `fastapi-nginx-sync-lambda-role`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ASGDescribe",
      "Effect": "Allow",
      "Action": [
        "autoscaling:DescribeAutoScalingGroups",
        "ec2:DescribeInstances"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SSMSendCommand",
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand",
        "ssm:GetCommandInvocation"
      ],
      "Resource": [
        "arn:aws:ssm:REGION::document/AWS-RunShellScript",
        "arn:aws:ec2:REGION:ACCOUNT_ID:instance/NGINX_PROXY_INSTANCE_ID"
      ]
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:REGION:ACCOUNT_ID:log-group:/aws/lambda/nginx-sync*"
    }
  ]
}
```

### 4.5 Security Group Rules Summary

| SG Name | Inbound Rule | Source | Port |
|---------|-------------|--------|------|
| `sg-app-asg` | Allow | `sg-nginx-proxy` SG ID | 8000 |
| `sg-app-asg` | Deny all other | `0.0.0.0/0` | ALL |
| `sg-nginx-proxy` | Allow HTTP | CloudFront IP prefixes | 80 |
| `sg-nginx-proxy` | Allow HTTPS | CloudFront IP prefixes | 443 |
| `sg-nginx-proxy` | Allow SSH | Your IP only | 22 |

> Use AWS-managed prefix list `com.amazonaws.global.cloudfront.origin-facing` for CloudFront IPs — it auto-updates.

---

## 5. Network Layout

```
VPC: 10.0.0.0/16  (default VPC is fine for demo)
├── Public Subnet A  10.0.1.0/24   us-east-1a
│   └── Nginx Proxy EC2 + Elastic IP
└── Public Subnet B  10.0.2.0/24   us-east-1b
    └── ASG instances (app) ← private IP only, no public IP needed
        (they pull ECR via NAT or VPC endpoint — see §6)
```

> **Cost Note on NAT Gateway:** NAT Gateway costs ~$32/mo — outside free tier.  
> **Alternative:** Assign public IPs to ASG instances (free) but block all inbound via SG. ECR traffic uses the public IP for egress only. This is safe because the SG blocks all public inbound.

---

## 6. ECR VPC Endpoint vs Public IP Strategy

Since NAT Gateway is not free-tier viable, ASG instances will:
1. Have **auto-assigned public IPs** (enabled in Launch Template)
2. SG blocks ALL inbound except from `sg-nginx-proxy`
3. Outbound to ECR uses the public IP (no extra charge within same region)

This is the correct cost-optimized approach for demo/free-tier workloads.
