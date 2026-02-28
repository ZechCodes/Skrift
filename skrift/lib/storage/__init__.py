"""Pluggable multi-backend asset storage."""

from skrift.lib.storage.base import StorageBackend, StoredFile
from skrift.lib.storage.local import LocalStorageBackend
from skrift.lib.storage.manager import StorageManager

__all__ = ["LocalStorageBackend", "StorageBackend", "StorageManager", "StoredFile"]
