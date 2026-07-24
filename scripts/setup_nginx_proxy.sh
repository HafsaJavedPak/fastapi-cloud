#!/bin/bash
set -euxo pipefail

dnf update -y
dnf install -y nginx
systemctl enable --now amazon-ssm-agent

# 1. Create the upstream configuration
cat > /etc/nginx/conf.d/upstream.conf <<'EOF'
upstream fastapi_backend {
    server 127.0.0.1:8000 max_fails=2 fail_timeout=10s;
}
EOF

# 2. Create the app.conf file (with the dangerous IP spoofing lines removed)
cat > /etc/nginx/conf.d/app.conf <<'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://fastapi_backend;
        
        # Standard proxy headers (Safe)
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

# Start Nginx now that the config files are in place
systemctl enable --now nginx
