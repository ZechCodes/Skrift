# Skrift Republish Demo

This demo runs two Skrift sites:

- **Source**: `http://localhost:8093`
  Requests a `republish` API grant and sends baseline repost payloads.
- **Target**: `http://localhost:8094`
  Enables API grants and the republish component, then accepts upsert/delete calls.

## Run

```bash
cd demo/republish-demo
docker compose up --build
```

Then open:

```text
http://localhost:8093
```

## Flow

1. Click **Authorize Republish** on the source site.
2. Sign in to the target with dummy auth when prompted.
3. On the target consent screen, choose:
   - page type: `post`
   - new/update behavior: `draft` or `publish`
   - deletion behavior: `unpublish`, `delete`, or `ignore`
4. Return to the source and click **Send Repost**.
5. Open the target admin posts page to see the created locked repost.
6. Click **Send Delete** to apply the selected deletion behavior.

## What It Covers

- The public `/.well-known/skrift` discovery document with republish metadata.
- A first-party `republish` permission requested through API grants.
- Grant-screen extension fields contributed by the republish component.
- API-key constraints for source origin, target page type, publish behavior, and delete behavior.
- `PUT /api/republish/posts` baseline upsert.
- `DELETE /api/republish/posts` deletion behavior.
- Locked reposts in target page admin.
- `/account` republish extension lists for inbound publishers and linked outbound destinations.
