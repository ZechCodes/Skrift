"""Tests for automatic CSP augmentation from storage backend origins."""

import pytest

from skrift.config import S3Config, StorageConfig, StoreConfig
from skrift.lib.csp import augment_csp, collect_storage_origins, extract_origin


class TestExtractOrigin:
    """Tests for extract_origin()."""

    def test_local_backend_returns_none(self):
        """Local backends are served from 'self', no CSP origin needed."""
        store = StoreConfig(backend="local")
        assert extract_origin(store) is None

    def test_custom_backend_returns_none(self):
        """Custom backends cannot be inferred."""
        store = StoreConfig(backend="mymodule:CustomBackend")
        assert extract_origin(store) is None

    def test_s3_with_public_url(self):
        """S3 store with a CDN public_url extracts the origin."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(public_url="https://cdn.example.com/assets"),
        )
        assert extract_origin(store) == "https://cdn.example.com"

    def test_s3_public_url_preserves_port(self):
        """Port numbers in public_url are preserved in the origin."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(public_url="https://cdn.example.com:8443/prefix"),
        )
        assert extract_origin(store) == "https://cdn.example.com:8443"

    def test_s3_public_url_strips_path(self):
        """Only scheme://host is kept; path components are stripped."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(public_url="https://cdn.example.com/deep/path/"),
        )
        assert extract_origin(store) == "https://cdn.example.com"

    def test_s3_private_without_public_url_returns_none(self):
        """Private S3 stores (presigned URLs) produce no origin."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(bucket="my-bucket", acl="private"),
        )
        assert extract_origin(store) is None

    def test_s3_public_read_with_endpoint_url(self):
        """Public-read S3 with custom endpoint (MinIO, R2) uses endpoint origin."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(
                acl="public-read",
                endpoint_url="https://minio.local:9000",
            ),
        )
        assert extract_origin(store) == "https://minio.local:9000"

    def test_s3_public_read_aws_default(self):
        """Public-read on AWS S3 constructs virtual-hosted-style origin."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(
                acl="public-read",
                bucket="my-assets",
                region="eu-west-1",
            ),
        )
        assert extract_origin(store) == "https://my-assets.s3.eu-west-1.amazonaws.com"

    def test_s3_public_url_takes_priority_over_acl(self):
        """When public_url is set, it takes priority regardless of acl."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(
                public_url="https://cdn.example.com",
                acl="public-read",
                endpoint_url="https://minio.local:9000",
            ),
        )
        assert extract_origin(store) == "https://cdn.example.com"

    def test_s3_public_read_no_bucket_returns_none(self):
        """Public-read on AWS without bucket/region cannot construct an origin."""
        store = StoreConfig(
            backend="s3",
            s3=S3Config(acl="public-read", bucket="", region=""),
        )
        assert extract_origin(store) is None


class TestAugmentCSP:
    """Tests for augment_csp()."""

    def test_empty_origins_returns_unchanged(self):
        """An empty directive_origins dict returns the CSP string unchanged."""
        csp = "default-src 'self'; img-src 'self' data:"
        assert augment_csp(csp, {}) == csp

    def test_appends_to_existing_directive(self):
        """Origins are appended to an existing directive."""
        csp = "default-src 'self'; img-src 'self' data:"
        result = augment_csp(csp, {"img-src": {"https://cdn.example.com"}})
        assert result == "default-src 'self'; img-src 'self' data: https://cdn.example.com"

    def test_inserts_missing_directive_after_default_src(self):
        """A missing directive is inserted after default-src with 'self'."""
        csp = "default-src 'self'; script-src 'self'"
        result = augment_csp(csp, {"font-src": {"https://cdn.example.com"}})
        assert result == "default-src 'self'; font-src 'self' https://cdn.example.com; script-src 'self'"

    def test_multiple_origins_sorted(self):
        """Multiple origins for one directive are sorted alphabetically."""
        csp = "default-src 'self'; img-src 'self'"
        result = augment_csp(csp, {"img-src": {"https://b.com", "https://a.com"}})
        assert result == "default-src 'self'; img-src 'self' https://a.com https://b.com"

    def test_multiple_directives(self):
        """Multiple directives are each augmented independently."""
        csp = "default-src 'self'; img-src 'self'; font-src 'self'"
        result = augment_csp(
            csp,
            {
                "img-src": {"https://cdn.example.com"},
                "font-src": {"https://cdn.example.com"},
            },
        )
        assert "img-src 'self' https://cdn.example.com" in result
        assert "font-src 'self' https://cdn.example.com" in result

    def test_mix_existing_and_missing_directives(self):
        """Handles both appending to existing and inserting new directives."""
        csp = "default-src 'self'; img-src 'self' data:"
        result = augment_csp(
            csp,
            {
                "img-src": {"https://cdn.example.com"},
                "style-src": {"https://cdn.example.com"},
            },
        )
        assert "img-src 'self' data: https://cdn.example.com" in result
        assert "style-src 'self' https://cdn.example.com" in result

    def test_no_default_src_inserts_at_start(self):
        """When default-src is absent, new directives are inserted at start."""
        csp = "img-src 'self'"
        result = augment_csp(csp, {"font-src": {"https://cdn.example.com"}})
        assert result.startswith("font-src 'self' https://cdn.example.com")


class TestCollectStorageOrigins:
    """Tests for collect_storage_origins()."""

    def test_all_local_stores_returns_empty(self):
        """Stores with only local backends produce no origins."""
        config = StorageConfig(
            stores={
                "default": StoreConfig(backend="local"),
                "uploads": StoreConfig(backend="local"),
            }
        )
        assert collect_storage_origins(config) == {}

    def test_s3_store_with_public_url(self):
        """An S3 store with a public_url produces origins for all default directives."""
        config = StorageConfig(
            stores={
                "cdn": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://cdn.example.com"),
                ),
            }
        )
        result = collect_storage_origins(config)
        expected_directives = {"img-src", "font-src", "style-src", "script-src"}
        assert set(result.keys()) == expected_directives
        for origins in result.values():
            assert origins == {"https://cdn.example.com"}

    def test_csp_directives_opt_out(self):
        """A store with csp_directives=[] produces no origins."""
        config = StorageConfig(
            stores={
                "cdn": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://cdn.example.com"),
                    csp_directives=[],
                ),
            }
        )
        assert collect_storage_origins(config) == {}

    def test_custom_csp_directives(self):
        """A store with restricted csp_directives only produces those directives."""
        config = StorageConfig(
            stores={
                "uploads": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://uploads.example.com"),
                    csp_directives=["img-src"],
                ),
            }
        )
        result = collect_storage_origins(config)
        assert set(result.keys()) == {"img-src"}
        assert result["img-src"] == {"https://uploads.example.com"}

    def test_multiple_stores_dedup_origins(self):
        """Two stores pointing to the same origin are deduplicated."""
        config = StorageConfig(
            stores={
                "media": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://cdn.example.com/media"),
                ),
                "assets": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://cdn.example.com/assets"),
                ),
            }
        )
        result = collect_storage_origins(config)
        # Same origin (scheme://host), should appear only once per directive
        for origins in result.values():
            assert origins == {"https://cdn.example.com"}

    def test_multiple_stores_different_origins(self):
        """Two stores with different origins both appear in the result."""
        config = StorageConfig(
            stores={
                "media": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://media.example.com"),
                ),
                "assets": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://assets.example.com"),
                ),
            }
        )
        result = collect_storage_origins(config)
        for origins in result.values():
            assert origins == {"https://media.example.com", "https://assets.example.com"}

    def test_mixed_local_and_s3_stores(self):
        """Local stores are skipped, only S3 stores contribute origins."""
        config = StorageConfig(
            stores={
                "default": StoreConfig(backend="local"),
                "cdn": StoreConfig(
                    backend="s3",
                    s3=S3Config(public_url="https://cdn.example.com"),
                ),
            }
        )
        result = collect_storage_origins(config)
        assert len(result) == 4  # all default directives
        for origins in result.values():
            assert origins == {"https://cdn.example.com"}
