"""How a failure of Google Cloud Storage becomes an `OSError`: the status of an
API error reply, the HTTP client's failures, and the translation of both."""

from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import json as _json
import sys as _sys

from . import _failures


def http_status(error: BaseException) -> "int | None":
    """The HTTP status of a `google.api_core.exceptions.GoogleAPICallError`
    (`NotFound.code == 404`, `Forbidden.code == 403`, ...), read from its
    `code` attribute so the SDK need not be importable to classify it. None
    for anything that is not an API error reply."""
    if isinstance(error, OSError):
        return None
    code = getattr(error, "code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def is_http_client_error(error: BaseException) -> bool:
    """Whether `error` comes from the HTTP client under the SDK (`requests`
    or `urllib3`), whose exceptions are what a refused connection, a timeout
    or a body cut short look like."""
    return any(
        cls.__module__.split(".")[0] in ("requests", "urllib3")
        for cls in type(error).__mro__
    )


def transport_kind(error: BaseException) -> str:
    """Which `_failures.transport_error()` kind a `requests`/`urllib3` failure is."""
    if _failures.mentions_timeout(error):
        return _failures.TIMEOUT
    names = {cls.__name__ for cls in type(error).__mro__}
    if names & {"ConnectionError", "NewConnectionError", "ProxyError", "SSLError"}:
        return _failures.UNREACHABLE
    if names & {
        "ChunkedEncodingError",
        "ContentDecodingError",
        "ProtocolError",
        "IncompleteRead",
    }:
        return _failures.INTERRUPTED
    return _failures.FAILED


def oserror(error: BaseException, path, *, create=False) -> "OSError | None":
    """The OSError an API error reply or a transport failure means for
    `path`, or None for any other exception (a missing SDK, bad credentials,
    a bug), which must propagate as itself rather than read as "no such
    file". `create`: the reply answers a conditional create, where 412 means
    the object exists. A request that ran out of time is TimeoutError, an
    endpoint that cannot be reached ConnectionError. Raise it `from None`."""
    # An error of this type exists only once the module that defines it is
    # loaded, so there is nothing to import here.
    api_exceptions = _sys.modules.get("google.api_core.exceptions")
    if isinstance(error, getattr(api_exceptions, "RetryError", ())):
        # The retry deadline passed: what the last attempt failed with is the
        # reason, and with nothing to say the deadline itself is a timeout.
        translated = (
            None if error.cause is None else oserror(error.cause, path, create=create)
        )
        return translated or _failures.transport_error(
            _failures.TIMEOUT, "GCS", path, error
        )
    status = http_status(error)
    if status is None:
        if is_http_client_error(error):
            return _failures.transport_error(transport_kind(error), "GCS", path, error)
        return None
    if status == 404:
        return FileNotFoundError(
            _errno.ENOENT, f"No such file or directory ({error})", str(path)
        )
    if status in (401, 403):
        return PermissionError(_errno.EACCES, str(error), str(path))
    if create and status == 412:
        return FileExistsError(_errno.EEXIST, f"File exists ({error})", str(path))
    if status in (408, 504):
        return _failures.transport_error(_failures.TIMEOUT, "GCS", path, error)
    return OSError(_errno.EIO, f"GCS request failed: {error}", str(path))


@_contextlib.contextmanager
def translate_errors(path, *, create=False):
    try:
        yield
    except Exception as error:
        translated = oserror(error, path, create=create)
        if translated is None:
            raise
        raise translated from None


class BatchReplyError(Exception):
    """A part of a batch reply that is not a success, shaped as the SDK's own
    API errors are: the HTTP status is `code`."""

    def __init__(self, code: int, reply):
        text = ""
        try:
            error = _json.loads(reply.content.decode("utf-8", "replace"))["error"]
            text = error.get("message") or ""
        except (ValueError, KeyError, AttributeError, TypeError):
            pass
        super().__init__(f"{code} {text or getattr(reply, 'reason', '') or 'error'}")
        self.code = code
