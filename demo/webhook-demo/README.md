# Skrift Webhook Demo

This demo runs two sites:

- **Sender**: a Skrift site that periodically enqueues outbound webhook calls.
- **Receiver**: a second site that logs incoming webhooks and can simulate delays,
  transient HTTP 500 errors, and permanent HTTP 410 failures.

Run it with Docker Compose or Podman Compose:

```bash
cd demo/webhook-demo
docker compose up --build
# or:
podman compose up --build
```

Open:

- Sender: <http://localhost:8084>
- Receiver: <http://localhost:8085>
- Webhook admin: <http://localhost:8084/admin/webhooks>
- Worker admin: <http://localhost:8084/admin/workers>

Use the receiver controls to switch between normal responses, transient errors,
permanent failures, response delays, and "fail next N" behavior. The sender's
webhook admin shows pending, retrying, dead, and succeeded deliveries, plus
failure summaries by endpoint domain and profile over time.

The compose setup runs migrations and a one-shot seed service before starting
the sender, so the demo opens directly instead of showing the setup wizard.
