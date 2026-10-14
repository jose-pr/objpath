"""The `s3:`, `gs:` and `az:` schemes of `uripath`: `S3URIPath`, `GSURIPath` and
`AZURIPath`, URI paths that delegate to `S3Path`, `GSPath` and `AZPath`.

Importing this module needs `uripath`: `pip install "objpath[uri]"`. `uripath`
loads it by itself, through the `uripath.schemes` entry points, the first time
a path of one of the schemes is used.
"""

from __future__ import annotations

try:
    import uripath as _uripath  # noqa: F401
except ImportError as _error:
    raise ImportError(
        'objpath.uri needs the uripath distribution: pip install "objpath[uri]"',
        name="uripath",
    ) from _error

import errno as _errno
import typing as _ty

from pathlib_next import FileStat
from pathlib_next import providers as _providers

from ._az import AZPath
from ._az_backend import BaseAZBackend
from ._az_backend import _default_backend as _az_default_backend
from ._az_errors import translate_errors as _az_translate_errors
from ._gs import GSPath
from ._gs_backend import BaseGSBackend
from ._gs_backend import _default_backend as _gs_default_backend
from ._s3_backend import BaseS3Backend
from ._s3_backend import _default_backend as _s3_default_backend
from ._uri_store import ObjectStoreURIPath

if _ty.TYPE_CHECKING:
    from ._s3 import S3Path

__all__ = ["AZURIPath", "GSURIPath", "S3URIPath"]


class S3URIPath(ObjectStoreURIPath):
    """`s3:` scheme (`s3://bucket/key/path`): read/write/list via `boto3`,
    done by an `S3Path` on the client of `backend` (a `BaseS3Backend`; without
    one, the backend every `s3:` path of the process shares). Needs the `s3`
    extra. See `S3Path` for the prefix emulation of directories."""

    __SCHEMES = ("s3",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseS3Backend

    @property
    def bucket(self) -> str:
        """The name of the bucket: the host of the URI."""
        return self.source.host

    def _initbackend(self):
        return _s3_default_backend()

    def _make_core(self) -> "S3Path":
        # Imported here: the module reads botocore's exceptions when it is
        # imported, and importing this one loads no SDK.
        from ._s3 import S3Path

        if not self.bucket:
            raise FileNotFoundError(
                _errno.ENOENT, "an s3: path needs a bucket", str(self)
            )
        return S3Path(self.path or "/", bucket=self.bucket, backend=self.backend)

    def _from_core(self, path: "S3Path") -> "S3URIPath":
        return self.with_path(f"/{path.key}")


class GSURIPath(ObjectStoreURIPath):
    """`gs:` scheme (`gs://bucket/key/path`): read/write/list via
    `google.cloud.storage`, done by a `GSPath` on the client of `backend` (a
    `BaseGSBackend`; without one, the backend every `gs:` path of the process
    shares). Needs the `gs` extra. See `GSPath` for the prefix emulation of
    directories."""

    __SCHEMES = ("gs",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseGSBackend

    @property
    def bucket(self) -> str:
        """The name of the bucket: the host of the URI."""
        return self.source.host

    def _initbackend(self):
        return _gs_default_backend()

    def _make_core(self) -> GSPath:
        if not self.bucket:
            raise FileNotFoundError(
                _errno.ENOENT, "a gs: path needs a bucket", str(self)
            )
        return GSPath(self.path or "/", bucket=self.bucket, backend=self.backend)

    def _from_core(self, path: GSPath) -> "GSURIPath":
        return self.with_path(f"/{path.key}")


class AZURIPath(ObjectStoreURIPath):
    """`az:` scheme (`az://account/container/key/path`): read/write/list via
    `azure.storage.blob`, done by an `AZPath` on the client of `backend` (a
    `BaseAZBackend`; without one, the backend every path of the account shares,
    authenticated by azure-identity's `DefaultAzureCredential`). Needs the `az`
    extra. The account has the containers as its directories; see `AZPath`
    for the prefix emulation of directories below them."""

    __SCHEMES = ("az",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseAZBackend

    @property
    def account(self) -> str:
        """The storage account: the host of the URI."""
        return self.source.host

    @property
    def container(self) -> str:
        """The name of the container: the first segment of the path, empty for
        the account itself."""
        return self.path.lstrip("/").split("/", 1)[0]

    def _key_text(self, uri) -> str:
        return "/" + uri.path.lstrip("/").partition("/")[2]

    def _initbackend(self):
        if not self.account:
            raise FileNotFoundError(
                _errno.ENOENT, "an az: path needs a storage account", str(self)
            )
        return _az_default_backend(self.account)

    def _make_core(self) -> AZPath:
        if not self.container:
            # The account holds containers, not blobs; its few operations are
            # written below.
            self.backend
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        return AZPath(
            self._key_text(self), container=self.container, backend=self.backend
        )

    def _from_core(self, path: AZPath) -> "AZURIPath":
        return self.with_path(f"/{self.container}/{path.key}")

    def _same_location(self, other) -> bool:
        # The authority is the storage account; a rename is a blob copy within
        # one container, so the container must match too.
        if not super()._same_location(other):
            return False
        other_container = next((s for s in other.path.split("/") if s), "")
        return other_container == self.container

    def _store_copy_possible(self, target) -> bool:
        # The service copies within one container, as rename() does.
        return super()._store_copy_possible(target) and self._same_location(target)

    def stat(self, *, follow_symlinks=True):
        if self.container:
            return super().stat(follow_symlinks=follow_symlinks)
        self._pop_stat_hint()
        # The account: ask the server whether it answers, as a container root
        # does. A one-item listing needs only the permission the rest uses.
        client = self.backend.client()
        with _az_translate_errors(self):
            for _ in client.list_containers(results_per_page=1):
                break
        return FileStat(is_dir=True)

    def _scandir(self):
        if self.container:
            yield from super()._scandir()
            return
        # The account: its containers are its directories.
        client = self.backend.client()
        with _az_translate_errors(self):
            names = [item.name for item in client.list_containers()]
        for name in names:
            if _providers.is_safe_child_name(name):
                yield name, FileStat(is_dir=True)

    def _mkdir(self, mode):
        if self.container:
            return super()._mkdir(mode)
        # The account exists, or stat() says it does not.
        self.stat()
        raise FileExistsError(_errno.EEXIST, "File exists", str(self))

    def rmdir(self):
        if self.container:
            return super().rmdir()
        self.stat()
        raise PermissionError(
            _errno.EACCES, "removing a container is not supported", str(self)
        )

    def _open(self, mode="r", buffering=-1):
        if not self.container:
            # The backend of an account-less path is the error it raises.
            self.backend
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        return super()._open(mode, buffering)
