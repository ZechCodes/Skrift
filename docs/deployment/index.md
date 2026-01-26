# Deployment

Deploy your Skrift site to production.

## Deployment Options

<div class="grid cards" markdown>

-   :material-server:{ .lg .middle } **Traditional Server**

    ---

    Deploy to a VPS or dedicated server with systemd and nginx.

    [:octicons-arrow-right-24: Production Guide](production.md)

-   :material-docker:{ .lg .middle } **Docker**

    ---

    Containerized deployment for cloud platforms.

    Coming soon

-   :material-cloud:{ .lg .middle } **Cloud Platforms**

    ---

    Deploy to Render, Railway, or Fly.io.

    Coming soon

</div>

## Quick Reference

### Production Checklist

- [ ] Generate secure `SECRET_KEY`
- [ ] Configure PostgreSQL database in `app.yaml`
- [ ] Set up HTTPS
- [ ] Update `auth.redirect_base_url` in `app.yaml`
- [ ] Run database migrations
- [ ] Set up process manager (systemd/supervisor)
- [ ] Configure reverse proxy (nginx/Caddy)

### Minimum Requirements

| Resource | Recommendation |
|----------|----------------|
| RAM | 512MB+ |
| CPU | 1 core+ |
| Storage | 1GB+ (plus database) |
| Python | 3.13+ |

## Configuration Differences

| Setting | Development | Production |
|---------|-------------|------------|
| `SECRET_KEY` | Any value | Secure random (env var) |
| `db.url` | SQLite | PostgreSQL |
| `auth.redirect_base_url` | `http://localhost:8080` | `https://yourdomain.com` |
