"""Pluggable multi-backend asset storage."""

from skrift.storage.base import StorageBackend, StoredFile
from skrift.storage.local import LocalStorageBackend
from skrift.storage.manager import StorageManager

__all__ = ["LocalStorageBackend", "StorageBackend", "StorageManager", "StoredFile"]
