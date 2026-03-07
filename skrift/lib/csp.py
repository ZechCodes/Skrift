"""CSP augmentation for storage backend origins.

Automatically adds external storage origins (S3, CDN) to the
Content-Security-Policy header so that assets served from configured
storage backends are not blocked by CSP.
"""

from __future__ import annotations

from urllib.parse import urlparse

from skrift.config import StorageConfig, StoreConfig


def extract_origin(store: StoreConfig) -> str | None:
    """Extract the CSP origin (scheme://host[:port]) from a store config.

    Returns ``None`` for local backends, presigned-only S3 stores, and
    custom backends whose origin cannot be inferred.
    """
    if store.backend != "s3":
        return None

    s3 = store.s3

    # Explicit CDN / public URL takes priority
    if s3.public_url:
        parsed = urlparse(s3.public_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return None

    # Private (presigned) stores should not add CSP origins
    if s3.acl != "public-read":
        return None

    # Public-read with custom endpoint (MinIO, R2, DigitalOcean Spaces, …)
    if s3.endpoint_url:
        parsed = urlparse(s3.endpoint_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return None

    # Public-read on AWS S3 — virtual-hosted-style URL
    if s3.bucket and s3.region:
        return f"https://{s3.bucket}.s3.{s3.region}.amazonaws.com"

    return None


def collect_storage_origins(
    storage_config: StorageConfig,
) -> dict[str, set[str]]:
    """Build a ``{directive: {origins}}`` mapping from all configured stores.

    Respects each store's ``csp_directives`` list.  Stores with an empty
    list or whose origin cannot be determined are skipped.
    """
    directive_origins: dict[str, set[str]] = {}

    for store in storage_config.stores.values():
        origin = extract_origin(store)
        if origin is None:
            continue
        for directive in store.csp_directives:
            directive_origins.setdefault(directive, set()).add(origin)

    return directive_origins


def augment_csp(csp: str, directive_origins: dict[str, set[str]]) -> str:
    """Append storage origins to directives in a CSP header string.

    * If a directive already exists, origins are appended.
    * If a directive is missing, it is inserted after ``default-src``
      with ``'self'`` plus the origins.

    Returns the CSP string unchanged when *directive_origins* is empty.
    """
    if not directive_origins:
        return csp

    parts = [p.strip() for p in csp.split(";") if p.strip()]

    # Map directive name → index for fast lookup
    def _build_index() -> dict[str, int]:
        idx: dict[str, int] = {}
        for i, part in enumerate(parts):
            tokens = part.split(None, 1)
            if tokens:
                idx[tokens[0]] = i
        return idx

    directive_map = _build_index()

    for directive, origins in directive_origins.items():
        sorted_origins = " ".join(sorted(origins))
        if directive in directive_map:
            idx = directive_map[directive]
            parts[idx] = f"{parts[idx]} {sorted_origins}"
        else:
            new_part = f"{directive} 'self' {sorted_origins}"
            if "default-src" in directive_map:
                insert_at = directive_map["default-src"] + 1
            else:
                insert_at = 0
            parts.insert(insert_at, new_part)
            # Re-index after insertion
            directive_map = _build_index()

    return "; ".join(parts)
