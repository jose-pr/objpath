"""How a failure of S3 becomes an `OSError`: botocore's exceptions, grouped by
what they mean, and the translation of an error reply."""

from __future__ import annotations

import errno as _errno

from . import _failures
from ._optional import import_or_stub

try:
    from boto3.exceptions import S3UploadFailedError
except ImportError:  # a botocore-only backend
    S3UploadFailedError = ()

# The `s3` extra. Without botocore the error tuples are empty (nothing to
# catch) and the first client the backend builds raises an ImportError naming
# the extra.
botoexc = import_or_stub("botocore.exceptions", "s3")
try:
    # botocore's connection-level failures: an endpoint that cannot be reached,
    # a timeout, a dropped connection, a body cut short. Not always an OSError,
    # so exists() and walk()'s on_error would not see them untranslated.
    TRANSPORT_ERRORS = (
        botoexc.ConnectionError,
        botoexc.HTTPClientError,
        botoexc.IncompleteReadError,
        botoexc.FlexibleChecksumError,
    )
    S3_ERRORS = (botoexc.ClientError,) + TRANSPORT_ERRORS
    TIMEOUT_ERRORS = (botoexc.ReadTimeoutError, botoexc.ConnectTimeoutError)
    INTERRUPTED_ERRORS = (
        botoexc.ConnectionClosedError,
        botoexc.ResponseStreamingError,
        botoexc.IncompleteReadError,
    )
except ImportError:
    TRANSPORT_ERRORS = S3_ERRORS = TIMEOUT_ERRORS = INTERRUPTED_ERRORS = ()


def error_code(error: botoexc.ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def http_status(error: botoexc.ClientError) -> "int | None":
    return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def is_not_found(error: botoexc.ClientError) -> bool:
    return reply_not_found(error_code(error), http_status(error))


def reply_not_found(code: str, status: "int | None") -> bool:
    return code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound") or status == 404


def is_precondition_failed(error: botoexc.ClientError) -> bool:
    # 412 PreconditionFailed: the key exists. 409 ConditionalRequestConflict:
    # a concurrent conditional write to the key is in progress.
    return (
        error_code(error) in ("PreconditionFailed", "ConditionalRequestConflict")
        or http_status(error) == 412
    )


def is_not_implemented(error: botoexc.ClientError) -> bool:
    return error_code(error) == "NotImplemented" or http_status(error) == 501


def reply_oserror(
    code: str, status: "int | None", message: str, path, action: "str | None" = None
) -> OSError:
    """The OSError the store's answer (`code`, HTTP `status`, `message`) means
    for `path`: not found (a key or a bucket) is FileNotFoundError, access
    denied PermissionError, anything else OSError(EIO). `action` names the
    request in the message of the last two."""
    if reply_not_found(code, status):
        return FileNotFoundError(
            _errno.ENOENT, f"No such file or directory ({code})", str(path)
        )
    head = f"S3 {action} failed: " if action else ""
    if code in ("403", "401", "AccessDenied", "AllAccessDisabled") or (
        status in (401, 403)
    ):
        return PermissionError(_errno.EACCES, f"{head}{code}: {message}", str(path))
    return OSError(_errno.EIO, f"{head or 'S3 '}{code}: {message}", str(path))


def oserror(error: Exception, path) -> OSError:
    """The OSError a botocore error means for `path`: the store's answer as
    `reply_oserror()` reads it; a request that ran out of time is
    TimeoutError, an endpoint that cannot be reached ConnectionError, a
    connection that broke mid-answer ConnectionResetError, anything else
    OSError. Raise it `from None`: the SDK's text can carry the request URL."""
    if isinstance(error, botoexc.ClientError):
        message = error.response.get("Error", {}).get("Message") or str(error)
        return reply_oserror(error_code(error), http_status(error), message, path)
    if isinstance(error, TIMEOUT_ERRORS):
        kind = _failures.TIMEOUT
    elif isinstance(error, INTERRUPTED_ERRORS):
        kind = _failures.INTERRUPTED
    elif isinstance(error, botoexc.ConnectionError):
        kind = _failures.UNREACHABLE
    else:
        kind = _failures.FAILED
    return _failures.transport_error(kind, "S3", path, error)
