"""Storage manager — registry of named storage backends."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from skrift.lib.storage.local import LocalStorageBackend

if TYPE_CHECKING:
    from pathlib import Path

    from skrift.config import StorageConfig, StoreConfig
    from skrift.lib.storage.base import StorageBackend


class StorageManager:
    """Registry that lazily creates and caches storage backends by name."""

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._backends: dict[str, StorageBackend] = {}

    @property
    def default_store(self) -> str:
        return self._config.default

    @property
    def store_names(self) -> list[str]:
        return list(self._config.stores.keys())

    async def get(self, name: str | None = None) -> StorageBackend:
        """Return the backend for *name*, creating it on first access."""
        name = name or self._config.default
        if name not in self._backends:
            store_cfg = self._config.stores.get(name)
            if store_cfg is None:
                raise KeyError(f"Unknown storage store: {name!r}")
            self._backends[name] = create_storage_backend(store_cfg, store_name=name)
        return self._backends[name]

    async def close(self) -> None:
        """Release resources held by backends."""
        for backend in self._backends.values():
            close = getattr(backend, "close", None)
            if close is not None:
                await close()
        self._backends.clear()


def create_storage_backend(config: StoreConfig, store_name: str = "default") -> StorageBackend:
    """Instantiate a storage backend from configuration."""
    from pathlib import Path

    backend_type = config.backend

    if backend_type == "local":
        return LocalStorageBackend(
            base_path=Path(config.local_path),
            store_name=store_name,
        )

    if backend_type == "s3":
        from skrift.lib.storage.s3 import S3StorageBackend

        return S3StorageBackend(config.s3)

    # Dynamic import: "module:ClassName"
    if ":" in backend_type:
        parts = backend_type.split(":")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid backend spec '{backend_type}': must contain exactly one colon"
            )
        module_path, class_name = parts
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls(config)

    raise ValueError(
        f"Unknown storage backend '{backend_type}'. "
        "Use 'local', 's3', or 'module:ClassName'."
    )
