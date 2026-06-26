# Media & Storage

Skrift stores uploaded files (images, documents, video) through a pluggable
**storage backend** and serves images at on-demand sizes that work the same
across every backend — local disk, S3-compatible object stores, or your own.

## Overview

- Files are uploaded through the admin media library or the asset service and
  recorded as `Asset` rows keyed by their content hash.
- Each store is backed by a **storage backend**: `local`, `s3`, or a custom
  class. Backends are addressed by name, so you can run several at once.
- Images can be requested at a named **size** (`thumb`, `medium`, …). Variants
  are generated lazily on first request, cached back into the same backend, and
  served from there afterward — including straight from a CDN for remote stores.

## Configuring stores

Storage is configured under the top-level `storage` key in `app.yaml`. Each
named entry under `stores` is a separate backend; `default` selects which store
new uploads use.

```yaml
storage:
  default: default
  stores:
    default:
      backend: local
      local_path: ./uploads
      max_upload_size: 10485760  # 10 MB
```

| Field | Default | Description |
|-------|---------|-------------|
| `backend` | `local` | `local`, `s3`, or `module:ClassName` for a custom backend |
| `local_path` | `./uploads` | Filesystem root for the `local` backend |
| `max_upload_size` | `10485760` | Per-file upload limit in bytes |
| `s3` | — | Nested S3 settings (see below), used when `backend: s3` |

### Local backend

The `local` backend stores files on disk under `local_path`, fanned out into
two levels of content-hash subdirectories. It serves files through Skrift at
`/storage/{store}/{key}` via `StorageFilesMiddleware`.

### S3 backend

The `s3` backend stores files in any S3-compatible bucket (AWS S3, MinIO,
Cloudflare R2, …). It requires the `s3` extra:

```bash
pip install skrift[s3]
```

```yaml
storage:
  default: cdn
  stores:
    cdn:
      backend: s3
      s3:
        bucket: my-bucket
        region: us-east-1
        acl: public-read
        public_url: https://cdn.example.com
```

| `s3` field | Default | Description |
|------------|---------|-------------|
| `bucket` | `""` | Target bucket name |
| `region` | `us-east-1` | AWS region |
| `prefix` | `""` | Key prefix applied to every object |
| `endpoint_url` | `""` | Custom endpoint for non-AWS providers (MinIO, R2) |
| `access_key_id` | `""` | Access key (omit to use the ambient AWS credential chain) |
| `secret_access_key` | `""` | Secret key |
| `acl` | `private` | Object ACL; `public-read` serves objects via their public S3 URL |
| `public_url` | `""` | CDN base URL placed in front of the bucket |
| `presign_ttl` | `3600` | Lifetime (seconds) of presigned URLs for private objects |

URL resolution for the S3 backend follows this order: a `public_url` CDN base if
set, then the public S3 URL for `public-read` buckets, otherwise a short-lived
presigned URL.

!!! warning "Never commit credentials"
    Prefer the ambient AWS credential chain (instance role, environment, or
    `~/.aws`) over `access_key_id`/`secret_access_key` in `app.yaml`. If you do
    set keys, keep them out of version control via environment-variable
    interpolation.

### Custom backends

Set `backend` to `module:ClassName` to load your own backend. The class is
instantiated with the `StoreConfig` and must satisfy the
`skrift.storage.base.StorageBackend` protocol — `put`, `get`, `delete`,
`exists`, `list_keys`, and `get_url`:

```yaml
storage:
  stores:
    default:
      backend: myapp.storage:GoogleCloudBackend
```

Image sizing, the media library, and the `image_url` helper all work
automatically with any compliant backend — there is no local-only path.

## On-demand image sizing

Request any image at a named size by appending `?size=<name>` to its
`/storage/...` URL. The available sizes are defined in
`skrift.lib.imaging.IMAGE_SIZES`:

| Size | Dimensions | Typical use |
|------|------------|-------------|
| `icon` | 64×64 | Favicons, avatars |
| `thumb` | 200×200 | Gallery and attachment thumbnails |
| `small` | 400 wide | Inline content images |
| `medium` | 800 wide | Gallery items, body images |
| `cover` | 1200 wide | Featured/hero images |
| `og` | 1200×630 | OpenGraph / social share images |

Sizes with a single dimension are width-constrained; the height follows the
aspect ratio. Resizing **never upscales** (smaller originals are returned
unchanged), preserves the original format, and re-encodes JPEG/WebP at quality
85.

### How a sized request flows

1. The first request for `…/{key}?size=thumb` reaches `StorageFilesMiddleware`.
2. The middleware reads the original through the backend, resizes it off the
   event loop, and writes the variant back into the **same backend** under a
   `{key}.{size}` key.
3. It then serves the result: bytes inline for backends with Skrift-internal
   URLs (local), or a `302` redirect to the backend's URL (S3/CDN).
4. Subsequent requests find the cached variant and skip regeneration.

This keeps generation off the hot path — the expensive resize happens once, and
warm requests for a remote store are served directly by the CDN.

## Resolving URLs in code

Use the asset service helpers from controllers and other server-side code
(`skrift/db/services/asset_service.py`):

```python
from skrift.db.services.asset_service import get_asset_url, image_url

storage = request.app.state.storage_manager

# Original, full-size URL (backend-native: local path or CDN/presigned URL)
url = await get_asset_url(storage, asset)

# Sized URL, resolved lazily and backend-aware
thumb = await image_url(storage, asset, "thumb")
```

`image_url` is the recommended way to produce sized image URLs:

- **No size** → the backend's direct URL for the original.
- **Variant already exists** → the backend's direct URL for the variant. For a
  CDN-backed store this is the CDN URL, so the browser never touches Skrift — no
  redirect.
- **Variant missing** → the internal `/storage/{store}/{key}?size=…` URL. The
  first visitor triggers generation and a redirect; the next render resolves to
  the direct URL.

It accepts either an `Asset` or a raw `key` (with an optional `store`):

```python
await image_url(storage, asset, "medium")
await image_url(storage, "abc123…", "medium", store="cdn")
```

## Resolving URLs in templates

!!! important "Templates render synchronously"
    Jinja templates in Skrift render synchronously, so the **async** helpers
    (`image_url`, `asset_url`) cannot be awaited inside a template — calling them
    there yields a coroutine, not a URL. Resolve URLs in your controller and
    pass the strings into the template context.

The built-in controllers already do this. Public page templates receive:

| Context variable | Contents |
|------------------|----------|
| `asset_urls` | `{asset_id: original_url}` for every attached asset |
| `asset_image_urls` | `{asset_id: medium_url}` for image assets (pre-resolved via `image_url`) |
| `featured_image_url` | Internal `/storage` URL for the featured asset (sized in-template for og:image) |
| `featured_cover_url` | Pre-resolved `cover` URL for on-page display |

```html
<!-- Gallery thumbnail — already resolved, warm path goes straight to the CDN -->
<img src="{{ asset_image_urls[asset.id | string] }}" alt="{{ asset.alt_text }}">

<!-- Featured cover -->
<img src="{{ featured_cover_url }}" alt="{{ page.title }}">

<!-- Full-size original (lightbox, downloads, video) -->
<a href="{{ asset_urls[asset.id | string] }}">Open</a>
```

### The `sized` filter

For URLs you size directly in a template — favicons and og:image — use the
`sized` filter, which appends `?size=<name>`:

```html
<link rel="icon" href="{{ favicon_url() | sized('icon') }}">
<meta property="og:image" content="{{ og_meta.image | sized('og') }}">
```

!!! note "Apply `sized` only to internal URLs"
    `sized` merely appends a query parameter, so it only triggers resizing when
    applied to a Skrift-internal `/storage/...` URL — appending `?size=` to a CDN
    URL does nothing. The built-in controllers pass internal URLs for the
    favicon and og:image precisely for this reason. For asset images, prefer the
    pre-resolved `image_url` context variables, which give you the warm CDN path.

## Key files

| File | Purpose |
|------|---------|
| `skrift/storage/base.py` | `StorageBackend` protocol, `StoredFile` |
| `skrift/storage/local.py` | Local filesystem backend |
| `skrift/storage/s3.py` | S3-compatible backend |
| `skrift/storage/manager.py` | `StorageManager`, backend factory |
| `skrift/lib/imaging.py` | `IMAGE_SIZES`, `resize_image()`, variant helpers |
| `skrift/middleware/storage.py` | `StorageFilesMiddleware` — serving and on-demand sizing |
| `skrift/db/services/asset_service.py` | Upload, delete, `get_asset_url`, `image_url` |
| `skrift/config.py` | `StorageConfig`, `StoreConfig`, `S3Config` |
