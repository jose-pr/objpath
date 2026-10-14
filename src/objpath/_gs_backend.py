"""The backends of `GSPath`: the contract and the `google-cloud-storage` one,
and the one every path built without a backend shares."""

from __future__ import annotations

import threading as _threading

from pathlib_next import providers as _providers

from ._clients import ClientBackend, LazyClientBackend
from ._optional import import_client

#: "Not given": `None` is a value for `retry=`.
UNSET = object()


class BaseGSBackend(ClientBackend):
    """Protocol for obtaining a `google.cloud.storage` Client. Subclass this
    to plug in custom credential/session handling (e.g. tests can override to
    point at a fake server); `GSBackend` is the real implementation."""

    __slots__ = ()

    @_providers.notimplemented
    def client(self):
        """The `google.cloud.storage` client every call of a path goes through."""

    def call_options(self) -> dict:
        """Keyword arguments (`timeout=`, `retry=`) added to every SDK call
        a path makes; none by default, which leaves the SDK's own."""
        return {}


class GSBackend(LazyClientBackend, BaseGSBackend):
    """Lazily creates and caches a `google.cloud.storage` Client. A single
    client is reused across threads -- it's documented as thread-safe.

    `client_kwargs` go to `storage.Client` unchanged, `client_options`
    (dict or `ClientOptions`, `api_endpoint` included) among them. A custom
    endpoint still authenticates; for an emulator or fake server pass
    `use_auth_w_custom_endpoint=False` (anonymous credentials), or set
    `STORAGE_EMULATOR_HOST` yourself -- the SDK reads it.

    `timeout` (seconds, or a `(connect, read)` pair) and `retry` (a
    `google.api_core.retry.Retry`, or None for no retry) are passed to every
    call a path makes; left out, the SDK's own defaults apply, which keep an
    unreachable endpoint waiting for about two minutes. A context manager:
    leaving the block closes the client."""

    __slots__ = ("client_kwargs", "_options")

    def __init__(self, *, timeout=UNSET, retry=UNSET, **client_kwargs):
        super().__init__()
        self.client_kwargs = client_kwargs
        self._options = {
            name: value
            for name, value in (("timeout", timeout), ("retry", retry))
            if value is not UNSET
        }

    def __repr__(self) -> str:
        return f"GSBackend({', '.join(sorted({*self.client_kwargs, *self._options}))})"

    def call_options(self) -> dict:
        return dict(self._options)

    def _build_client(self):
        storage = import_client("google.cloud.storage", "gs")
        # Passed through as given: turning a dict `api_endpoint` into a
        # process-wide `os.environ["STORAGE_EMULATOR_HOST"]` would redirect
        # every later client in the process, and any subprocess, to it.
        return storage.Client(**self.client_kwargs)


_DEFAULT_BACKEND: "GSBackend | None" = None
_DEFAULT_BACKEND_LOCK = _threading.Lock()


def _default_backend() -> GSBackend:
    """The one backend, and so the one client, that every `GSPath` built
    without a backend shares in this process."""
    global _DEFAULT_BACKEND
    with _DEFAULT_BACKEND_LOCK:
        if _DEFAULT_BACKEND is None:
            _DEFAULT_BACKEND = GSBackend()
        return _DEFAULT_BACKEND


def _reset_default_backend() -> None:
    """Forget the shared default backend, so the next path builds a new one.
    The test suite calls it between tests; paths that already hold the old
    one keep it."""
    global _DEFAULT_BACKEND
    with _DEFAULT_BACKEND_LOCK:
        _DEFAULT_BACKEND = None
