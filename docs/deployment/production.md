# Production Deployment

Complete guide for deploying Skrift to a production environment.

!!! warning "Before You Deploy"
    Review the [Security Checklist](security-checklist.md) before going live. It covers critical items like secret keys, OAuth configuration, and HTTPS setup.

## Prerequisites

- Linux server (Ubuntu 22.04+ recommended)
- Python 3.13+
- PostgreSQL 14+
- Domain name with DNS configured
- SSL certificate (Let's Encrypt recommended)

## Step 1: Server Setup

### Install Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and dependencies
sudo apt install python3.13 python3.13-venv python3-pip -y

# Install PostgreSQL
sudo apt install postgresql postgresql-contrib -y

# Install nginx
sudo apt install nginx -y
```

### Create Application User

```bash
sudo useradd -m -s /bin/bash skrift
sudo su - skrift
```

## Step 2: Database Setup

### Create PostgreSQL Database

```bash
sudo -u postgres psql
```

```sql
CREATE USER skrift WITH PASSWORD 'your-secure-password';
CREATE DATABASE skrift OWNER skrift;
\q
```

## Step 3: Application Setup

### Clone and Install

```bash
# As skrift user
cd /home/skrift

# Clone repository (or download release)
git clone https://github.com/ZechCodes/Skrift.git app
cd app

# Create virtual environment
python3.13 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e .
```

### Configure app.yaml

Create `/home/skrift/app/app.yaml`:

```yaml
db:
  url: $DATABASE_URL

auth:
  redirect_base_url: https://yourdomain.com
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET

controllers:
  - skrift.controllers.auth:AuthController
  - skrift.admin.controller:AdminController
  - skrift.controllers.web:WebController
```

The `$VAR_NAME` syntax references environment variables, keeping secrets out of the config file.

### Run Migrations

```bash
source venv/bin/activate
skrift-db upgrade head
```

## Step 4: Process Manager

### Create Systemd Service

Create `/etc/systemd/system/skrift.service`:

```ini
[Unit]
Description=Skrift Web Application
After=network.target postgresql.service

[Service]
User=skrift
Group=skrift
WorkingDirectory=/home/skrift/app
Environment="PATH=/home/skrift/app/venv/bin"
Environment="SECRET_KEY=your-secure-secret-key"
Environment="DATABASE_URL=postgresql+asyncpg://skrift:your-secure-password@localhost:5432/skrift"
Environment="GOOGLE_CLIENT_ID=your-production-client-id"
Environment="GOOGLE_CLIENT_SECRET=your-production-client-secret"
ExecStart=/home/skrift/app/venv/bin/hypercorn skrift.asgi:app \
    --workers 4 \
    --bind 127.0.0.1:8000 \
    --access-logfile /var/log/skrift/access.log \
    --error-logfile /var/log/skrift/error.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Generate a secure secret key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Create Log Directory

```bash
sudo mkdir -p /var/log/skrift
sudo chown skrift:skrift /var/log/skrift
```

### Enable and Start Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable skrift
sudo systemctl start skrift
sudo systemctl status skrift
```

## Step 5: Reverse Proxy

### Nginx Configuration

Create `/etc/nginx/sites-available/skrift`:

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    # SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;

    # Security headers - most are handled at the application level by Skrift's
    # built-in SecurityHeadersMiddleware. Add nginx-specific headers here if needed.
    # add_header X-Frame-Options "SAMEORIGIN" always;
    # add_header X-Content-Type-Options "nosniff" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /home/skrift/app/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

### Enable Site

```bash
sudo ln -s /etc/nginx/sites-available/skrift /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### SSL with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d yourdomain.com
```

## Step 6: Update OAuth Settings

Update your OAuth provider settings:

1. Go to your provider's developer console
2. Edit your OAuth application
3. Add production redirect URI: `https://yourdomain.com/auth/google/callback`
4. Save changes

## Maintenance

### Deploying Updates

```bash
cd /home/skrift/app
git pull
source venv/bin/activate
pip install -e .
skrift-db upgrade head
sudo systemctl restart skrift
```

### Viewing Logs

```bash
# Application logs
sudo journalctl -u skrift -f

# Access logs
tail -f /var/log/skrift/access.log

# Error logs
tail -f /var/log/skrift/error.log
```

### Database Backups

```bash
# Create backup
pg_dump -U skrift skrift > backup_$(date +%Y%m%d).sql

# Restore backup
psql -U skrift skrift < backup_20240101.sql
```

## Performance Tuning

### Gunicorn Workers

Adjust workers based on CPU cores:

```bash
# Rule: 2-4 workers per core
# For 2 cores: 4-8 workers
gunicorn ... -w 4
```

### PostgreSQL Tuning

Edit `/etc/postgresql/14/main/postgresql.conf`:

```ini
shared_buffers = 256MB
effective_cache_size = 768MB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 7864kB
default_statistics_target = 100
random_page_cost = 1.1
effective_io_concurrency = 200
min_wal_size = 1GB
max_wal_size = 4GB
```

## Security Checklist

- [x] HTTPS enabled with valid certificate
- [x] `DEBUG` not set or set to `false`
- [x] Secure `SECRET_KEY` environment variable
- [x] Database password is strong
- [x] Firewall configured (allow 80, 443, 22 only)
- [x] Regular security updates applied
- [x] Database backups scheduled
- [x] Log rotation configured

## Troubleshooting

### Service Won't Start

Check logs:

```bash
sudo journalctl -u skrift -n 50
```

Common issues:

- Wrong file permissions
- Missing environment variables
- Database connection refused

### 502 Bad Gateway

The application isn't responding:

```bash
# Check if service is running
sudo systemctl status skrift

# Check if port is listening
ss -tlnp | grep 8000
```

### Database Connection Error

```bash
# Test connection
psql -U skrift -h localhost -d skrift

# Check PostgreSQL is running
sudo systemctl status postgresql
```

## See Also

- [Security Checklist](security-checklist.md) - Pre-deployment verification
- [Environment Variables](../reference/environment-variables.md) - Configuration reference
- [OAuth Providers](../reference/auth-providers.md) - OAuth setup
