"""S3-compatible storage — deliberately unimplemented.

It exists to prove the seam is real: swapping ``LocalStorage`` for this is a
configuration change, not a refactor. Roughly forty lines of boto3 whenever an
S3 endpoint (AWS, or a local Floci/LocalStack emulator) is actually wanted.
"""

from __future__ import annotations


class S3Storage:
    def __init__(self, bucket: str, endpoint_url: str | None = None) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url

    def put(self, key: str, data: bytes) -> str:
        raise NotImplementedError("S3Storage is a seam placeholder; use LocalStorage")

    def get(self, uri: str) -> bytes:
        raise NotImplementedError("S3Storage is a seam placeholder; use LocalStorage")

    def exists(self, uri: str) -> bool:
        raise NotImplementedError("S3Storage is a seam placeholder; use LocalStorage")

    def delete(self, uri: str) -> None:
        raise NotImplementedError("S3Storage is a seam placeholder; use LocalStorage")
