"""The backends of `S3Path`: the contract and the `boto3` one, and the one
every path built without a backend shares."""

from __future__ import annotations

import threading as _threading

from pathlib_next import providers as _providers

from ._clients import ClientBackend, LazyClientBackend
from ._optional import import_client


class BaseS3Backend(ClientBackend):
    """Protocol for obtaining a `boto3` S3 client. Subclass this to plug in
    custom credential/session handling (e.g. tests mock it directly, no
    real AWS account needed); `S3Backend` is the real implementation."""

    __slots__ = ()

    @_providers.notimplemented
    def client(self):
        """The `boto3` S3 client every call of a path goes through."""


class S3Backend(LazyClientBackend, BaseS3Backend):
    """Lazily creates and caches a `boto3` S3 client. A single `boto3` client
    is reused across threads -- it's documented as thread-safe.

    `client_kwargs` go to `boto3.client("s3", ...)` unchanged (`region_name`,
    `endpoint_url`, `aws_access_key_id`, `config`, ...). A context manager:
    leaving the block closes the client."""

    __slots__ = ("client_kwargs",)

    def __init__(self, **client_kwargs):
        super().__init__()
        self.client_kwargs = client_kwargs

    def __repr__(self) -> str:
        return f"S3Backend({', '.join(sorted(self.client_kwargs))})"

    def _build_client(self):
        boto3 = import_client("boto3", "s3")
        return boto3.client("s3", **self.client_kwargs)


_DEFAULT_BACKEND: "S3Backend | None" = None
_DEFAULT_BACKEND_LOCK = _threading.Lock()


def _default_backend() -> S3Backend:
    """The one backend, and so the one client and connection pool, that every
    `S3Path` built without a backend shares in this process."""
    global _DEFAULT_BACKEND
    with _DEFAULT_BACKEND_LOCK:
        if _DEFAULT_BACKEND is None:
            _DEFAULT_BACKEND = S3Backend()
        return _DEFAULT_BACKEND


def _reset_default_backend() -> None:
    """Forget the shared default backend, so the next path builds a new one.
    The test suite calls it between tests; paths that already hold the old
    one keep it."""
    global _DEFAULT_BACKEND
    with _DEFAULT_BACKEND_LOCK:
        _DEFAULT_BACKEND = None
