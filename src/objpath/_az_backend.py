"""The backends of `AZPath`: the contract and the `azure-storage-blob` one,
and the one every path built for an account without a backend shares."""

from __future__ import annotations

import threading as _threading
import urllib.parse as _urlparse

from pathapi import providers as _providers

from ._clients import ClientBackend, LazyClientBackend
from ._optional import import_client


class BaseAZBackend(ClientBackend):
    """Protocol for obtaining an `azure.storage.blob.BlobServiceClient`.
    Subclass this to plug in custom credential/session handling (e.g. tests
    can override to point at a fake server); `AZBackend` is the real
    implementation. `account`, when a subclass sets it, is the storage
    account the client reaches."""

    __slots__ = ()

    account: "str | None" = None

    #: Bytes of a blob that one request of a streamed read asks for. The SDK
    #: holds about nine times a response while it handles it: a larger number
    #: trades that memory for fewer requests.
    read_chunk = 512 * 1024

    #: Seconds `AZPath.rename()` and a server-side `copy()` wait for the service
    #: copy to finish. A copy inside one account is normally done when it is
    #: started; past this the copy is aborted (the destination may still appear
    #: if the abort fails) and the call raises `TimeoutError`, the source
    #: untouched.
    copy_timeout = 300.0

    @_providers.notimplemented
    def client(self):
        """The `BlobServiceClient` every call of a path goes through."""


def _default_credential():
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as error:
        raise ImportError(
            "an AZPath built with account= and no credential authenticates with "
            "azure-identity's DefaultAzureCredential, which is not installed: "
            'pip install "objpath[az]" (the az extra), or pass '
            "backend=AZBackend(account_url=..., credential=...) or "
            "AZBackend(connection_string=...)"
        ) from error
    return DefaultAzureCredential()


def _account_named_by(client_kwargs: dict) -> "str | None":
    """The storage account that `connection_string=` (`AccountName`) or
    `account_url=` (the first label of a `*.blob.core.windows.net` host, else
    the first path segment of an emulator's URL) names, or None."""
    connection = client_kwargs.get("connection_string")
    if connection:
        for part in str(connection).split(";"):
            name, _, value = part.partition("=")
            if name.strip().lower() == "accountname" and value:
                return value
    url = client_kwargs.get("account_url")
    if url:
        parsed = _urlparse.urlparse(str(url))
        host = parsed.hostname or ""
        if host.endswith(".blob.core.windows.net"):
            return host.split(".")[0]
        return parsed.path.strip("/").split("/")[0] or None
    return None


class AZBackend(LazyClientBackend, BaseAZBackend):
    """Lazily creates and caches an `azure.storage.blob.BlobServiceClient`. A
    single client is reused across threads -- it's documented as thread-safe.

    `client_kwargs` go to `BlobServiceClient` unchanged; with
    `connection_string`, to `BlobServiceClient.from_connection_string`
    (`credential` and the other options included). `account`, when given
    and neither `account_url` nor `connection_string` is, derives
    `account_url="https://<account>.blob.core.windows.net"`, authenticated
    by azure-identity's `DefaultAzureCredential` unless `credential` is
    passed -- the backend an `AZPath` built with `account=` uses. Without
    `account`, the account is the one `connection_string` or `account_url`
    names, when they name one. A context manager: leaving the block closes the
    client."""

    __slots__ = ("client_kwargs", "account")

    def __init__(self, account: "str | None" = None, **client_kwargs):
        super().__init__()
        self.client_kwargs = client_kwargs
        self.account = (
            account if account is not None else _account_named_by(client_kwargs)
        )

    def __repr__(self) -> str:
        shown = [] if self.account is None else [f"account={self.account!r}"]
        return f"AZBackend({', '.join([*shown, *sorted(self.client_kwargs)])})"

    def _build_client(self):
        BlobServiceClient = import_client("azure.storage.blob", "az").BlobServiceClient

        kwargs = dict(self.client_kwargs)
        if "connection_string" in kwargs:
            connection_string = kwargs.pop("connection_string")
            return BlobServiceClient.from_connection_string(connection_string, **kwargs)
        if "account_url" not in kwargs and self.account:
            kwargs["account_url"] = f"https://{self.account}.blob.core.windows.net"
            if "credential" not in kwargs:
                kwargs["credential"] = _default_credential()
        return BlobServiceClient(**kwargs)


_DEFAULT_BACKENDS: "dict[str, AZBackend]" = {}
_DEFAULT_BACKENDS_LOCK = _threading.Lock()


def _default_backend(account: str) -> AZBackend:
    """One shared default backend per account, so separately built paths
    reuse one client (and one credential) instead of building their own."""
    with _DEFAULT_BACKENDS_LOCK:
        backend = _DEFAULT_BACKENDS.get(account)
        if backend is None:
            backend = _DEFAULT_BACKENDS[account] = AZBackend(account=account)
        return backend
