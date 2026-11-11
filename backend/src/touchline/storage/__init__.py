"""Lazy AWS access and bounded private-object download authorization."""

from .s3 import PresignedObject, S3Access, StorageAuthorizationError

__all__ = ["PresignedObject", "S3Access", "StorageAuthorizationError"]
