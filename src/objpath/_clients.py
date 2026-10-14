"""What the backend of every store shares: the context manager, the client
built on first use, and what a pickled or copied backend keeps."""

from __future__ import annotations

import threading as _threading

#: Backend attributes that are the live client and what guards it: left out
#: of a pickled or copied backend, which builds its own on first use.
_LIVE = frozenset({"_client", "_client_lock", "__dict__", "__weakref__"})


def lazy_client(backend, build):
    """`backend._client`, made by `build()` the first time under
    `backend._client_lock` so that threads racing for it build one."""
    client = backend._client
    if client is None:
        with backend._client_lock:
            client = backend._client
            if client is None:
                client = backend._client = build()
    return client


def backend_state(backend) -> dict:
    """What a pickled or copied backend keeps: its slots and `__dict__`,
    without the client it built and the lock that guarded the build."""
    state = {}
    for cls in type(backend).__mro__:
        slots = getattr(cls, "__slots__", ())
        for name in (slots,) if isinstance(slots, str) else slots:
            if name not in _LIVE and hasattr(backend, name):
                state[name] = getattr(backend, name)
    state.update(
        (name, value)
        for name, value in getattr(backend, "__dict__", {}).items()
        if name not in _LIVE
    )
    return state


def restore_backend(backend, state: dict) -> None:
    """The inverse of `backend_state()`: a backend with no client yet."""
    for name, value in state.items():
        setattr(backend, name, value)
    backend._client = None
    backend._client_lock = _threading.Lock()


class ClientBackend(object):
    """The base of the backend contract of every store: a context manager
    whose `close()` releases what the backend holds, and the `picklable` flag.

    `picklable` says whether a path that holds the backend pickles it. It is
    `False`: the options of a client carry credentials, so a pickled path
    names its place only and the receiving process builds the default backend.
    """

    # Weakly referenceable so a path can be told which backends it built.
    __slots__ = ("__weakref__",)

    picklable = False

    def close(self) -> None:
        """Releases what this backend holds. Nothing here; a backend that holds
        a client closes it. Harmless when called again; the backend stays
        usable."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class LazyClientBackend(ClientBackend):
    """A backend that builds its SDK client the first time it is asked for it
    (`_build_client()`), once however many threads ask first, and keeps it.

    `close()` closes the client when the SDK's has a `close()`, and the next
    `client()` builds a new one. A pickled or copied backend keeps its
    options and builds its own client."""

    __slots__ = ("_client", "_client_lock")

    def __init__(self) -> None:
        self._client = None
        self._client_lock = _threading.Lock()

    def client(self):
        """The SDK client every call of a path goes through."""
        return lazy_client(self, self._build_client)

    def _build_client(self):
        raise NotImplementedError

    def close(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()

    def __getstate__(self):
        return backend_state(self)

    def __setstate__(self, state):
        restore_backend(self, state)
