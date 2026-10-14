"""How a failure of Azure Blob Storage becomes an `OSError`: the status of an
error reply, the SDK's report of a connection failure, and the translation of
both."""

from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import sys as _sys

from . import _failures


def http_status(error: BaseException) -> "int | None":
    """The HTTP status of an `azure.core.exceptions.HttpResponseError`
    (`ResourceNotFoundError`, `ResourceExistsError`, ...), read from its
    `status_code` attribute -- or its type when the SDK set none -- so the
    SDK need not be importable to classify it. None for anything that is
    not an error reply."""
    if isinstance(error, OSError):
        return None
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    try:
        from azure.core import exceptions as _azexc
    except ImportError:
        return None
    if isinstance(error, _azexc.ResourceNotFoundError):
        return 404
    if isinstance(error, _azexc.ResourceExistsError):
        return 409
    if isinstance(error, _azexc.ClientAuthenticationError):
        return 401
    if isinstance(error, _azexc.HttpResponseError):
        return 500
    return None


def transport_kind(error: BaseException) -> "str | None":
    """Which `_failures.transport_error()` kind `error` is when it is the SDK's
    report of a connection failure (not of a reply), else None."""
    # An SDK error exists only once its module is loaded, so there is nothing
    # to import here.
    azexc = _sys.modules.get("azure.core.exceptions")
    if azexc is None:
        return None
    timeouts = tuple(
        getattr(azexc, name)
        for name in ("ServiceRequestTimeoutError", "ServiceResponseTimeoutError")
        if hasattr(azexc, name)
    )
    if isinstance(error, timeouts):
        return _failures.TIMEOUT
    if isinstance(error, azexc.ServiceRequestError):
        # The request could not be sent: no connection, no name, no route.
        return (
            _failures.TIMEOUT
            if _failures.mentions_timeout(error)
            else _failures.UNREACHABLE
        )
    if isinstance(error, azexc.ServiceResponseError):
        # Sent, but the answer did not arrive whole.
        return (
            _failures.TIMEOUT
            if _failures.mentions_timeout(error)
            else _failures.INTERRUPTED
        )
    incomplete = getattr(azexc, "IncompleteReadError", None)
    if incomplete is not None and isinstance(error, incomplete):
        return _failures.INTERRUPTED
    return None


def oserror(error: BaseException, path, *, create=False):
    """The OSError an error reply or a connection failure means for `path`,
    or None for any other exception (a missing SDK, bad configuration, a
    bug), which must propagate as itself rather than read as "no such
    file". `create`: the reply answers a conditional create, where 409/412
    mean the blob exists (elsewhere a 412 is e.g. a lease held by someone
    else). A request that ran out of time is TimeoutError, an endpoint that
    cannot be reached ConnectionError. Raise it `from None`."""
    kind = transport_kind(error)
    if kind is not None:
        return _failures.transport_error(kind, "Azure", path, error)
    status = http_status(error)
    if status is None:
        return None
    if status == 404:
        return FileNotFoundError(
            _errno.ENOENT, f"No such file or directory ({error})", str(path)
        )
    if status in (401, 403):
        return PermissionError(_errno.EACCES, str(error), str(path))
    if create and status in (409, 412):
        return FileExistsError(_errno.EEXIST, f"File exists ({error})", str(path))
    if status in (408, 504):
        return _failures.transport_error(_failures.TIMEOUT, "Azure", path, error)
    return OSError(_errno.EIO, f"Azure request failed: {error}", str(path))


@_contextlib.contextmanager
def translate_errors(path, *, create=False):
    try:
        yield
    except Exception as error:
        translated = oserror(error, path, create=create)
        if translated is None:
            raise
        raise translated from None
