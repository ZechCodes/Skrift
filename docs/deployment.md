# Skrift Deployment Guide

This guide covers deploying Skrift in various configurations, from minimal VPS deployments to production-ready Docker setups.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Minimal VPS Deployment](#minimal-vps-deployment)
- [Docker Deployment](#docker-deployment)
- [Docker Compose with PostgreSQL](#docker-compose-with-postgresql)
- [Ephemeral Docker Deployment](#ephemeral-docker-deployment)
- [Reverse Proxy Configuration](#reverse-proxy-configuration)
- [SSL/TLS Setup](#ssltls-setup)
- [Monitoring and Logging](#monitoring-and-logging)
- [Backup and Recovery](#backup-and-recovery)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

Before deploying Skrift, ensure you have:

- A server with at least 512MB RAM (1GB+ recommended)
- Python 3.13+ or Docker installed
- Domain name (optional but recommended for OAuth)
- OAuth credentials from at least one provider (Google, GitHub, etc.)

---

## Minimal VPS Deployment

This section covers deploying Skrift directly on a VPS with minimal configuration.

### Step 1: Server Setup

Update your system and install Python:

```bash
# Ubuntu/Debian
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.13 python3.13-venv python3-pip
```

### Step 2: Install Skrift

Create a project directory and install Skrift:

```bash
# Create project directory
sudo mkdir -p /opt/skrift
sudo chown $USER:$USER /opt/skrift
cd /opt/skrift

# Create virtual environment and install
python3.13 -m venv .venv
source .venv/bin/activate
pip install skrift
```

### Step 3: Configure Environment

Create your environment file:

```bash
# Generate a secure secret key
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# Create .env file
cat > .env << EOF
SECRET_KEY=$SECRET_KEY
DEBUG=false
DATABASE_URL=sqlite+aiosqlite:///./app.db
OAUTH_REDIRECT_BASE_URL=https://yourdomain.com
EOF
```

Add your OAuth credentials:

```bash
cat >> .env << EOF
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
EOF
```

### Step 4: Run Initial Setup

Start the application to complete the setup wizard:

```bash
cd /opt/skrift
source .venv/bin/activate
skrift
```

Access `http://your-server-ip:8080` and complete the setup wizard. This creates an `app.yaml` configuration file. Once complete, stop the development server (Ctrl+C).

**Alternative: Manual Configuration**

Instead of using the wizard, you can create `app.yaml` manually:

```yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.admin.controller:AdminController
  - skrift.controllers.web:WebController

db:
  url: $DATABASE_URL

auth:
  redirect_base_url: $OAUTH_REDIRECT_BASE_URL
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
      scopes: [openid, email, profile]
```

Then run migrations:

```bash
skrift-db upgrade head
```

### Step 5: Create Systemd Service

Create a systemd service for production:

```bash
sudo tee /etc/systemd/system/skrift.service << EOF
[Unit]
Description=Skrift Web Application
After=network.target

[Service]
Type=exec
User=www-data
Group=www-data
WorkingDirectory=/opt/skrift
EnvironmentFile=/opt/skrift/.env
ExecStart=/opt/skrift/.venv/bin/gunicorn skrift.asgi:app -w 2 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Install gunicorn and set permissions:

```bash
cd /opt/skrift
source .venv/bin/activate
pip install gunicorn

# Set ownership
sudo chown -R www-data:www-data /opt/skrift

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable skrift
sudo systemctl start skrift

# Check status
sudo systemctl status skrift
```

### Step 6: Configure Nginx (Reverse Proxy)

Install and configure Nginx:

```bash
sudo apt install -y nginx

sudo tee /etc/nginx/sites-available/skrift << EOF
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /static/ {
        alias /opt/skrift/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/skrift /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### Step 7: Enable HTTPS with Let's Encrypt

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

Your site is now live at `https://yourdomain.com`.

---

## Docker Deployment

### Basic Docker Deployment

Create a `Dockerfile` in your project directory:

```dockerfile
# Dockerfile
FROM python:3.13-slim

WORKDIR /app

# Install Skrift
RUN pip install --no-cache-dir skrift gunicorn

# Create data directory for SQLite
RUN mkdir -p /app/data

# Copy configuration (if pre-configured)
# COPY app.yaml .

EXPOSE 8080

CMD ["gunicorn", "skrift.asgi:app", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8080"]
```

Build and run:

```bash
# Build
docker build -t mysite .

# Run (setup wizard mode - no app.yaml)
docker run -d \
  --name mysite \
  -p 8080:8080 \
  -v mysite-data:/app/data \
  -v mysite-config:/app/config \
  -e SECRET_KEY="your-secret-key" \
  mysite

# Access http://localhost:8080 to complete setup wizard
```

After completing setup, the container will have an `app.yaml` in the config volume.

### Running with Pre-configured app.yaml

For automated deployments, create `app.yaml` beforehand:

```bash
# Run with pre-configured app.yaml
docker run -d \
  --name mysite \
  -p 8080:8080 \
  -v $(pwd)/app.yaml:/app/app.yaml:ro \
  -v mysite-data:/app/data \
  -e SECRET_KEY="your-secret-key" \
  -e DATABASE_URL="sqlite+aiosqlite:///./data/app.db" \
  -e OAUTH_REDIRECT_BASE_URL="https://yourdomain.com" \
  -e GOOGLE_CLIENT_ID="your-client-id" \
  -e GOOGLE_CLIENT_SECRET="your-client-secret" \
  mysite

# Run migrations
docker exec mysite skrift-db upgrade head
```

---

## Docker Compose with PostgreSQL

For production deployments, use Docker Compose with PostgreSQL.

First, create a `Dockerfile`:

```dockerfile
# Dockerfile
FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir skrift gunicorn

EXPOSE 8080
CMD ["gunicorn", "skrift.asgi:app", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8080"]
```

Create `docker-compose.yml`:

```yaml
# docker-compose.yml
services:
  skrift:
    build: .
    ports:
      - "8080:8080"
    environment:
      - SECRET_KEY=${SECRET_KEY}
      - DEBUG=false
      - DATABASE_URL=postgresql+asyncpg://skrift:${DB_PASSWORD}@db:5432/skrift
      - OAUTH_REDIRECT_BASE_URL=${OAUTH_REDIRECT_BASE_URL}
      - GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}
      - GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}
      - GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID}
      - GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET}
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ./app.yaml:/app/app.yaml:ro
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      - POSTGRES_USER=skrift
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_DB=skrift
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U skrift"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

volumes:
  postgres-data:
```

Create your `.env` file:

```bash
# .env for docker-compose
SECRET_KEY=your-secret-key-here
DB_PASSWORD=your-secure-db-password
OAUTH_REDIRECT_BASE_URL=https://yourdomain.com
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GITHUB_CLIENT_ID=your-github-client-id
GITHUB_CLIENT_SECRET=your-github-client-secret
```

Create `app.yaml` (see [Manual Configuration](#manual-configuration) in docs/README.md).

Deploy:

```bash
# Start services
docker compose up -d

# View logs
docker compose logs -f

# Run migrations
docker compose exec skrift skrift-db upgrade head
```

---

## Ephemeral Docker Deployment

This configuration uses ephemeral containers with external PostgreSQL storage. Ideal for:
- Container orchestration (Kubernetes, Docker Swarm)
- Auto-scaling environments
- Stateless deployments

### Architecture

```
┌─────────────────┐     ┌─────────────────┐
│   Load Balancer │────►│   Skrift Pod 1  │
└────────┬────────┘     └────────┬────────┘
         │                       │
         │              ┌────────┴────────┐
         │              │   Skrift Pod 2  │
         │              └────────┬────────┘
         │                       │
         │              ┌────────┴────────┐
         │              │   Skrift Pod N  │
         │              └────────┬────────┘
         │                       │
         │              ┌────────▼────────┐
         └─────────────►│   PostgreSQL    │
                        │   (External)    │
                        └─────────────────┘
```

### docker-compose.ephemeral.yml

```yaml
# docker-compose.ephemeral.yml
services:
  skrift:
    build: .
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
        reservations:
          cpus: '0.25'
          memory: 256M
    environment:
      - SECRET_KEY=${SECRET_KEY}
      - DEBUG=false
      - DATABASE_URL=${DATABASE_URL}
      - OAUTH_REDIRECT_BASE_URL=${OAUTH_REDIRECT_BASE_URL}
      # OAuth credentials via environment
      - GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}
      - GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}
    # No volumes - completely ephemeral
    # Configuration injected via environment
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./certs:/etc/nginx/certs:ro
    depends_on:
      - skrift
    restart: unless-stopped
```

### Preconfigured app.yaml

For ephemeral deployments, pre-configure `app.yaml` with environment variable references:

```yaml
# app.yaml - Preconfigured for ephemeral deployment
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.admin.controller:AdminController
  - skrift.controllers.web:WebController

db:
  url: $DATABASE_URL
  pool_size: 5
  pool_overflow: 10
  pool_timeout: 30
  echo: false

auth:
  redirect_base_url: $OAUTH_REDIRECT_BASE_URL
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
      scopes:
        - openid
        - email
        - profile
    github:
      client_id: $GITHUB_CLIENT_ID
      client_secret: $GITHUB_CLIENT_SECRET
      scopes:
        - read:user
        - user:email
```

### Dockerfile for Ephemeral Deployment

```dockerfile
# Dockerfile.ephemeral
FROM python:3.13-slim

WORKDIR /app

# Install Skrift and dependencies
RUN pip install --no-cache-dir skrift gunicorn curl

# Copy preconfigured app.yaml
COPY app.yaml .

# No volume mounts needed - everything from environment

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:8080/ || exit 1

# Run with multiple workers
CMD ["gunicorn", "skrift.asgi:app", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8080"]
```

### Deployment Steps

1. **Set up external PostgreSQL** (managed database recommended):

```bash
# Create database
createdb -h your-db-host -U postgres skrift

# Note your connection string
# postgresql+asyncpg://user:password@host:5432/skrift
```

2. **Prepare environment**:

```bash
# .env
SECRET_KEY=your-very-secure-secret-key
DATABASE_URL=postgresql+asyncpg://user:password@db-host:5432/skrift
OAUTH_REDIRECT_BASE_URL=https://yourdomain.com
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GITHUB_CLIENT_ID=your-github-client-id
GITHUB_CLIENT_SECRET=your-github-client-secret
```

3. **Run migrations once** (before scaling):

```bash
docker compose -f docker-compose.ephemeral.yml run --rm skrift \
  skrift-db upgrade head
```

4. **Create initial admin** (if not using setup wizard):

```bash
# Connect to one container and complete setup via browser
docker compose -f docker-compose.ephemeral.yml up -d --scale skrift=1

# Complete setup at https://yourdomain.com/setup

# Then scale up
docker compose -f docker-compose.ephemeral.yml up -d --scale skrift=3
```

5. **Deploy and scale**:

```bash
docker compose -f docker-compose.ephemeral.yml up -d
```

### Kubernetes Deployment

Build and push your Docker image, then create these manifests:

```bash
# Build and push image
docker build -f Dockerfile.ephemeral -t your-registry/mysite:latest .
docker push your-registry/mysite:latest
```

```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: skrift
spec:
  replicas: 3
  selector:
    matchLabels:
      app: skrift
  template:
    metadata:
      labels:
        app: skrift
    spec:
      containers:
        - name: skrift
          image: your-registry/mysite:latest
          ports:
            - containerPort: 8080
          envFrom:
            - secretRef:
                name: skrift-secrets
            - configMapRef:
                name: skrift-config
          resources:
            requests:
              memory: "256Mi"
              cpu: "250m"
            limits:
              memory: "512Mi"
              cpu: "500m"
          livenessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 40
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: skrift
spec:
  selector:
    app: skrift
  ports:
    - port: 80
      targetPort: 8080
  type: ClusterIP
```

```yaml
# k8s/secrets.yaml
apiVersion: v1
kind: Secret
metadata:
  name: skrift-secrets
type: Opaque
stringData:
  SECRET_KEY: "your-secret-key"
  DATABASE_URL: "postgresql+asyncpg://user:pass@postgres:5432/skrift"
  GOOGLE_CLIENT_SECRET: "your-google-secret"
  GITHUB_CLIENT_SECRET: "your-github-secret"
```

```yaml
# k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: skrift-config
data:
  DEBUG: "false"
  OAUTH_REDIRECT_BASE_URL: "https://yourdomain.com"
  GOOGLE_CLIENT_ID: "your-google-client-id"
  GITHUB_CLIENT_ID: "your-github-client-id"
```

---

## Reverse Proxy Configuration

### Nginx Configuration

Full nginx configuration for production:

```nginx
# /etc/nginx/nginx.conf
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {
    worker_connections 1024;
    use epoll;
    multi_accept on;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

    access_log /var/log/nginx/access.log main;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    types_hash_max_size 2048;

    # Gzip compression
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml application/json application/javascript
               application/xml application/xml+rss text/javascript;

    # Upstream for load balancing
    upstream skrift {
        least_conn;
        server skrift:8080;
        # Add more servers for scaling:
        # server skrift-2:8080;
        # server skrift-3:8080;
        keepalive 32;
    }

    # Redirect HTTP to HTTPS
    server {
        listen 80;
        server_name yourdomain.com;
        return 301 https://$host$request_uri;
    }

    # HTTPS server
    server {
        listen 443 ssl http2;
        server_name yourdomain.com;

        # SSL configuration
        ssl_certificate /etc/nginx/certs/fullchain.pem;
        ssl_certificate_key /etc/nginx/certs/privkey.pem;
        ssl_session_timeout 1d;
        ssl_session_cache shared:SSL:50m;
        ssl_session_tickets off;

        # Modern SSL configuration
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
        ssl_prefer_server_ciphers off;

        # HSTS
        add_header Strict-Transport-Security "max-age=63072000" always;

        # Security headers
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-XSS-Protection "1; mode=block" always;

        # Static files with caching
        location /static/ {
            alias /app/static/;
            expires 30d;
            add_header Cache-Control "public, immutable";
        }

        # Proxy to Skrift
        location / {
            proxy_pass http://skrift;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";

            # Timeouts
            proxy_connect_timeout 60s;
            proxy_send_timeout 60s;
            proxy_read_timeout 60s;
        }
    }
}
```

### Caddy Configuration

For simpler SSL management, use Caddy:

```caddyfile
# Caddyfile
yourdomain.com {
    reverse_proxy skrift:8080

    file_server /static/* {
        root /app
    }

    encode gzip

    header {
        X-Frame-Options "SAMEORIGIN"
        X-Content-Type-Options "nosniff"
        X-XSS-Protection "1; mode=block"
        Strict-Transport-Security "max-age=63072000"
    }
}
```

---

## SSL/TLS Setup

### Let's Encrypt with Certbot

For VPS deployments:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com

# Auto-renewal (usually automatic)
sudo certbot renew --dry-run
```

### Let's Encrypt with Docker

Use the certbot Docker image:

```yaml
# docker-compose with certbot
services:
  certbot:
    image: certbot/certbot
    volumes:
      - ./certs:/etc/letsencrypt
      - ./certbot-www:/var/www/certbot
    command: certonly --webroot -w /var/www/certbot -d yourdomain.com --email you@email.com --agree-tos --non-interactive

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./certs:/etc/nginx/certs:ro
      - ./certbot-www:/var/www/certbot:ro
```

---

## Monitoring and Logging

### Log Collection

Configure logging in docker-compose:

```yaml
services:
  skrift:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### Health Checks

Built-in health check endpoint:

```bash
# Check if service is healthy
curl -f http://localhost:8080/ || echo "Service unhealthy"
```

### Prometheus Metrics (optional)

Add metrics endpoint by creating a custom controller:

```python
# myapp/controllers/metrics.py
from litestar import Controller, get
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from litestar.response import Response

class MetricsController(Controller):
    path = "/metrics"

    @get("/")
    async def metrics(self) -> Response:
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST
        )
```

---

## Backup and Recovery

### PostgreSQL Backup

```bash
# Backup
docker compose exec db pg_dump -U skrift skrift > backup-$(date +%Y%m%d).sql

# Restore
docker compose exec -T db psql -U skrift skrift < backup-20240115.sql
```

### Automated Backup Script

```bash
#!/bin/bash
# backup.sh
BACKUP_DIR="/backups"
DATE=$(date +%Y%m%d_%H%M%S)
DB_CONTAINER="skrift-db-1"

# Create backup
docker exec $DB_CONTAINER pg_dump -U skrift skrift | gzip > "$BACKUP_DIR/skrift_$DATE.sql.gz"

# Keep only last 7 days
find $BACKUP_DIR -name "skrift_*.sql.gz" -mtime +7 -delete

echo "Backup completed: skrift_$DATE.sql.gz"
```

Add to crontab:

```bash
# Daily backup at 2 AM
0 2 * * * /opt/skrift/backup.sh >> /var/log/skrift-backup.log 2>&1
```

---

## Troubleshooting

### Common Issues

**Application won't start:**
```bash
# Check logs
docker compose logs skrift
# or
sudo journalctl -u skrift -f

# Check if database is reachable
docker compose exec skrift uv run python -c "
from sqlalchemy import create_engine
import os
engine = create_engine(os.environ['DATABASE_URL'].replace('+asyncpg', ''))
engine.connect()
print('Database connection successful')
"
```

**OAuth redirect errors:**
```bash
# Verify OAUTH_REDIRECT_BASE_URL matches your domain
echo $OAUTH_REDIRECT_BASE_URL

# Check OAuth provider console for correct callback URLs
# Should be: https://yourdomain.com/auth/{provider}/callback
```

**Permission denied errors:**
```bash
# Fix file permissions
sudo chown -R www-data:www-data /opt/skrift
sudo chmod -R 755 /opt/skrift
```

**Database migration fails:**
```bash
# Check current migration state
docker compose exec skrift uv run skrift-db current

# Reset and reapply (CAUTION: data loss)
docker compose exec skrift uv run skrift-db downgrade base
docker compose exec skrift uv run skrift-db upgrade head
```

**Container runs out of memory:**
```yaml
# Increase memory limits in docker-compose.yml
deploy:
  resources:
    limits:
      memory: 1G
```

### Getting Help

- Check application logs first
- Review this documentation
- Search for similar issues on GitHub
- Open an issue with:
  - Error messages
  - Steps to reproduce
  - Environment details (OS, Docker version, etc.)
