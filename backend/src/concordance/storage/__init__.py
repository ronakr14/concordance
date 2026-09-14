"""Blob storage backends behind the ``StorageBackend`` protocol."""

from concordance.storage.local import LocalStorage
from concordance.storage.s3 import S3Storage

__all__ = ["LocalStorage", "S3Storage"]
