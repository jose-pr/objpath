"""The exceptions `objpath` defines.

A failure of a store or of the connection to it is the `OSError` type that
`pathlib` raises for the same condition (`FileNotFoundError`,
`PermissionError`, `TimeoutError`, `ConnectionError`, ...); this module holds
the two that name a cause no builtin does. An exception of a store's SDK is
never raised across the API. A bad argument is a plain `ValueError` or
`TypeError`.
"""

from __future__ import annotations

from pathlib_next import PathError

__all__ = ["ObjPathError", "ObjectChangedError", "UnreachableKeysError"]


class ObjPathError(PathError):
    """Base class of the exceptions `objpath` defines for a failure at run
    time that no builtin describes."""


class ObjectChangedError(ObjPathError, OSError):
    """An object was replaced or deleted while a stream was reading it (errno
    `ESTALE`, else `EIO` where the platform has none): a read is one version
    of the object and never the bytes of two."""


class UnreachableKeysError(ObjPathError, OSError):
    """A copy or a removal of a prefix was refused because the prefix holds
    keys a listing does not show (errno `EINVAL`); nothing was changed."""
