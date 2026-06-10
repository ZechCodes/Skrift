# Republish API

Skrift can accept baseline cross-site reposts from another service through API permission grants. The component is optional and issues a constrained service key for one source origin, one target page type, and selected publish/delete behavior.

## Configuration

```yaml
api_keys:
  enabled: true

api_grants:
  enabled: true

republish:
  enabled: true
  discovery_enabled: true
  default_page_type: post
  page_types: [post]
  default_post_behavior: draft      # draft or publish
  default_delete_behavior: unpublish # unpublish, delete, or ignore
```

When a service requests the `republish` permission, the grant screen asks the user which page type to create, whether incoming posts should publish or stay draft, and how source deletions should be handled. These choices are stored as API-key constraints.

## Discovery

When discovery is enabled, `GET /.well-known/skrift` includes a `republish` section, and `GET /api/republish/capabilities` returns the supported page types, behaviors, endpoints, baseline schema name, and media mode.

## Baseline Payload

Use the grant-issued API key with `Authorization: Bearer sk_...`.

```bash
curl -X PUT https://site.example/api/republish/posts \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "canonical_url": "https://source.example/posts/hello",
    "title": "Hello",
    "content": "<p>Hello from another site.</p>",
    "summary": "Short description",
    "author_name": "A. Writer",
    "updated_at": "2026-05-22T12:00:00Z",
    "image_url": "https://source.example/media/hello.jpg",
    "tags": ["example"]
  }'
```

`canonical_url` is the stable upsert key. Its origin must match the source origin captured during authorization, so a key issued to `https://source.example` cannot edit reposts from another origin. Media is hotlinked in the baseline implementation.

To apply the configured source-deletion behavior:

```bash
curl -X DELETE https://site.example/api/republish/posts \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"canonical_url": "https://source.example/posts/hello"}'
```

Republished pages are locked in the normal page-type admin. They remain viewable, but direct edits, publishing, unpublishing, deletion, and revision restore are blocked so the source service remains authoritative.

## Account Page

When the component is enabled, Skrift adds a republishing section to `/account`.
The section includes a `Link Site` action, the destination sites this account has
linked for publishing, and the source sites that have been granted permission to
publish into this site.
