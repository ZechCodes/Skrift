---
name: skrift-assets
description: "Skrift asset storage system — pluggable backends (local, S3), CLI management, image variants, and automatic CSP integration."
---

# Skrift Assets & Storage

Skrift provides a pluggable storage system for managing uploaded files (images, documents, media) with named stores, content deduplication, and automatic CSP header integration.

## Architecture Overview

```
StoreConfig (app.yaml)
  → StorageManager (lazy backend registry)
    → StorageBackend (local / S3 / custom)
      → StoredFile (key, url, content_type, size, hash)

Asset model (DB) tracks metadata + links files to pages
StorageFilesMiddleware serves local files at /storage/{store}/{key}
CSP augmentation adds external origins to Content-Security-Policy at startup
```

## Storage Backends

### StorageBackend Protocol (`skrift/lib/storage/base.py`)

```python
class StorageBackend(Protocol):
    async def put(key, data, content_type) -> StoredFile
    async def get(key) -> bytes
    async def delete(key) -> None
    async def exists(key) -> bool
    async def list_keys(prefix="") -> AsyncIterator[str]
    async def get_url(key) -> str
```

### Local Backend (`skrift/lib/storage/local.py`)

- Stores files in `{local_path}/{hash_prefix}/{key}`
- Served by `StorageFilesMiddleware` at `/storage/{store}/{key}`
- Supports on-demand image resizing via `?size=name` query parameter

### S3 Backend (`skrift/lib/storage/s3.py`)

- Uses `aioboto3` for async S3 operations
- Supports any S3-compatible service (AWS, MinIO, R2, DigitalOcean Spaces)
- URL resolution priority:
  1. `public_url` (CDN/CloudFront) → `{public_url}/{prefix}/{key}`
  2. `acl: public-read` → virtual-hosted-style S3 URL
  3. `acl: private` → presigned URL with configurable TTL

### Custom Backend

Use `backend: "module:ClassName"` for custom implementations. The class receives the `StoreConfig` as its constructor argument and must implement the `StorageBackend` protocol.

## Configuration (`skrift/config.py`)

```python
class S3Config(BaseModel):
    bucket: str = ""
    region: str = "us-east-1"
    prefix: str = ""
    endpoint_url: str = ""          # For S3-compatible services
    access_key_id: str = ""
    secret_access_key: str = ""
    acl: str = "private"            # "private" | "public-read"
    public_url: str = ""            # CDN base URL
    presign_ttl: int = 3600         # Presigned URL expiration (seconds)

class StoreConfig(BaseModel):
    backend: str = "local"          # "local" | "s3" | "module:ClassName"
    local_path: str = "./uploads"
    max_upload_size: int = 10_485_760  # 10 MB
    s3: S3Config = S3Config()
    csp_directives: list[str] = ["img-src", "font-src", "style-src", "script-src"]

class StorageConfig(BaseModel):
    default: str = "default"
    stores: dict[str, StoreConfig] = {"default": StoreConfig()}
```

### Example app.yaml

```yaml
storage:
  default: cdn
  stores:
    cdn:
      backend: s3
      s3:
        bucket: my-site-assets
        region: us-east-1
        access_key_id: ${AWS_ACCESS_KEY_ID}
        secret_access_key: ${AWS_SECRET_ACCESS_KEY}
        public_url: https://cdn.example.com

    user-uploads:
      backend: s3
      s3:
        bucket: my-site-uploads
        region: us-east-1
        public_url: https://uploads.example.com
      csp_directives: [img-src]  # Only trust for images
```

## StorageManager (`skrift/lib/storage/manager.py`)

Lazy registry that creates backends on first access:

```python
from skrift.lib.storage import StorageManager

manager = StorageManager(settings.storage)
backend = await manager.get("cdn")         # Get named backend
backend = await manager.get()              # Get default backend
names = manager.store_names                # List configured stores
await manager.close()                      # Release all backend resources
```

The manager is stored on `app.state.storage_manager` and accessible in request handlers via `request.app.state.storage_manager`.

## Asset Model (`skrift/db/models/asset.py`)

Database model tracking uploaded files:

| Field | Type | Description |
|-------|------|-------------|
| `key` | str | Storage key (content-hash based) |
| `store` | str | Store name this asset lives in |
| `content_hash` | str | SHA-256 of file content (dedup key) |
| `filename` | str | Original upload filename |
| `content_type` | str | MIME type |
| `size` | int | File size in bytes |
| `folder` | str | Virtual folder for organization |
| `alt_text` | str | Alt text for images |
| `user_id` | int | Uploading user |

## Asset Service (`skrift/db/services/asset_service.py`)

```python
from skrift.db.services.asset_service import (
    upload_asset,       # Upload with content deduplication
    delete_asset,       # Delete from DB + backend (if no other references)
    get_asset_url,      # Resolve URL via backend
    list_assets,        # List with filtering
    count_assets,       # Count with filtering
    sync_page_assets,   # Link assets to pages
)
```

## Storage Middleware (`skrift/middleware/storage.py`)

Serves locally-stored assets at `/storage/{store}/{key}`:

- Only serves `backend="local"` stores (remote backends use their own URLs)
- Path traversal prevention and null byte rejection
- On-demand image resizing: `/storage/default/photo.jpg?size=thumbnail`
- Resized variants cached alongside originals

## CLI Commands (`skrift/cli.py`)

```
skrift storage stores                          # List configured stores
skrift storage ls [--store NAME]               # List assets in database
skrift storage orphans [--store NAME] [--delete]  # Find files without DB rows
skrift storage sync --source DIR [--store NAME] [--delete] [--dry-run]
skrift storage migrate --from NAME --to NAME [--dry-run]
```

## Automatic CSP Integration (`skrift/lib/csp.py`)

At startup, Skrift automatically adds external storage origins to the CSP header so assets from S3/CDN backends are not blocked.

### How It Works

1. `collect_storage_origins()` iterates all configured stores and extracts the origin (scheme://host) from each S3 store's URL configuration
2. `augment_csp()` appends those origins to the directives listed in each store's `csp_directives` field
3. This happens once at startup in `create_app()` — no per-request overhead

### Origin Extraction Rules

| Configuration | Origin used |
|---|---|
| `public_url` set | Origin from `public_url` |
| `acl: public-read` + `endpoint_url` | Origin from `endpoint_url` |
| `acl: public-read` on AWS | `https://{bucket}.s3.{region}.amazonaws.com` |
| `acl: private` (presigned) | None |
| `backend: local` | None (served from `'self'`) |

### The `csp_directives` Field

Controls which CSP directives receive the store's origin:

```yaml
# Default — allow all asset types
csp_directives: [img-src, font-src, style-src, script-src]

# User uploads — images only (prevent XSS via uploaded HTML/JS)
csp_directives: [img-src]

# Opt out entirely
csp_directives: []
```

### API

```python
from skrift.lib.csp import (
    extract_origin,            # (StoreConfig) -> str | None
    collect_storage_origins,   # (StorageConfig) -> dict[str, set[str]]
    augment_csp,               # (csp_str, directive_origins) -> str
)
```

## Key Files

| File | Purpose |
|------|---------|
| `skrift/config.py` | `S3Config`, `StoreConfig`, `StorageConfig` models |
| `skrift/lib/storage/base.py` | `StorageBackend` protocol, `StoredFile` dataclass |
| `skrift/lib/storage/local.py` | Local filesystem backend |
| `skrift/lib/storage/s3.py` | S3-compatible backend |
| `skrift/lib/storage/manager.py` | `StorageManager` registry |
| `skrift/lib/csp.py` | CSP origin extraction and augmentation |
| `skrift/middleware/storage.py` | Local file serving middleware |
| `skrift/db/models/asset.py` | Asset database model |
| `skrift/db/services/asset_service.py` | Asset CRUD operations |
| `skrift/cli.py` | Storage CLI commands |
| `skrift/asgi.py` | CSP augmentation wiring in `create_app()` |
