"""How a failure of the connection to a store becomes an `OSError`, and how a
stream tells that its object changed."""

from __future__ import annotations

import errno as _errno

from .exceptions import ObjectChangedError

#: What a failure of the connection to the store is, as `transport_error()` raises
#: it: the request ran out of time, the endpoint was unreachable, the connection
#: broke after the store began to answer, or anything else the HTTP client reported.
TIMEOUT, UNREACHABLE, INTERRUPTED, FAILED = (
    "timeout",
    "unreachable",
    "interrupted",
    "failed",
)

_TRANSPORT = {
    TIMEOUT: (TimeoutError, _errno.ETIMEDOUT, "request timed out"),
    UNREACHABLE: (ConnectionError, _errno.EHOSTUNREACH, "endpoint unreachable"),
    INTERRUPTED: (
        ConnectionResetError,
        _errno.ECONNRESET,
        "connection lost before the response was complete",
    ),
    FAILED: (OSError, _errno.EIO, "request failed"),
}


def transport_error(kind: str, store: str, path, error: BaseException) -> OSError:
    """The `OSError` for a transport failure of `store` ("S3", "GCS", "Azure")
    on `path`: `TimeoutError`, `ConnectionError`, `ConnectionResetError` or
    plain `OSError` by `kind`, with the path as `filename`. Only the type name
    of `error` reaches the message: an HTTP client's own text carries the
    request URL, and with it any query-string credential. Raise it
    `from None`."""
    cls, number, text = _TRANSPORT[kind]
    return cls(number, f"{store} {text} ({type(error).__name__})", str(path))


def mentions_timeout(error: BaseException) -> bool:
    """Whether `error`, or an exception it wraps (`__cause__`, `__context__`,
    an SDK's `inner_exception` or `cause`, a `reason` or an argument), is a
    timeout of any HTTP client: a type named `Timeout`, `ReadTimeout`,
    `ConnectTimeoutError`, ..., or `TimeoutError` itself. The client libraries
    are matched by name so none has to be imported. urllib3's
    `NewConnectionError` derives from its `ConnectTimeoutError` but means the
    connection was refused or the name did not resolve: it is not one."""
    pending = [error]
    for _ in range(8):
        if not pending:
            break
        current = pending.pop()
        if current is None:
            continue
        names = {cls.__name__ for cls in type(current).__mro__}
        if "NewConnectionError" in names:
            continue
        if any(
            name.endswith(("Timeout", "TimeoutError")) or name == "timeout"
            for name in names
        ):
            return True
        for attr in ("__cause__", "__context__", "inner_exception", "cause", "reason"):
            pending.append(getattr(current, attr, None))
        pending.extend(
            arg
            for arg in getattr(current, "args", ())
            if isinstance(arg, BaseException)
        )
    return False


def changed_error(path) -> ObjectChangedError:
    """The error for a stream whose object was replaced while it was being
    read: a read is one version of the object, and what follows the change is
    another's. `ESTALE` where the platform has it, else `EIO`."""
    return ObjectChangedError(
        getattr(_errno, "ESTALE", _errno.EIO),
        "the object changed while it was being read",
        str(path),
    )
