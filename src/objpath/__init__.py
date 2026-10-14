"""pathlib-style paths for S3, Google Cloud Storage and Azure Blob Storage.

`S3Path`, `GSPath` and `AZPath` need no URI. None of them imports its SDK
until a client is built, and none is imported until it is first named
(PEP 562), so `import objpath` needs no extra and never imports `uripath`. The
`s3:`, `gs:` and `az:` schemes of `uripath` are `objpath.uri`.
"""

from __future__ import annotations

import importlib as _importlib
import typing as _ty

from ._version import __version__
from .exceptions import ObjectChangedError, ObjPathError, UnreachableKeysError

if _ty.TYPE_CHECKING:
    from ._az import AZPath as AZPath
    from ._az_backend import AZBackend as AZBackend
    from ._az_backend import BaseAZBackend as BaseAZBackend
    from ._gs import GSPath as GSPath
    from ._gs_backend import BaseGSBackend as BaseGSBackend
    from ._gs_backend import GSBackend as GSBackend
    from ._s3 import S3Path as S3Path
    from ._s3_backend import BaseS3Backend as BaseS3Backend
    from ._s3_backend import S3Backend as S3Backend

__all__ = [
    "AZBackend",
    "AZPath",
    "BaseAZBackend",
    "BaseGSBackend",
    "BaseS3Backend",
    "GSBackend",
    "GSPath",
    "ObjPathError",
    "ObjectChangedError",
    "S3Backend",
    "S3Path",
    "UnreachableKeysError",
    "__version__",
]

#: Public name -> the private module that defines it, imported on first use.
_LAZY = {
    "AZBackend": "._az_backend",
    "AZPath": "._az",
    "BaseAZBackend": "._az_backend",
    "BaseGSBackend": "._gs_backend",
    "BaseS3Backend": "._s3_backend",
    "GSBackend": "._gs_backend",
    "GSPath": "._gs",
    "S3Backend": "._s3_backend",
    "S3Path": "._s3",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
