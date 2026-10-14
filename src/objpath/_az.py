from __future__ import annotations

import errno as _errno
import io as _io
import itertools as _itertools
import time as _time
import typing as _ty

from pathapi import FileStat
from pathapi import providers as _providers

from . import _failures
from ._az_backend import (
    _DEFAULT_BACKENDS,
    AZBackend,
    BaseAZBackend,
    _default_backend,
)
from ._az_errors import http_status, oserror, translate_errors
from ._optional import import_client as _import_client
from ._reader import RangeReader
from ._places import check_place
from ._storepath import ObjectStorePath
from ._writer import SPOOL_SIZE, SpoolUploadStream

_AZWriteStream = SpoolUploadStream


class _ReadOnly:
    """`file` seen through `read()` alone. The SDK uploads a stream it
    cannot measure in blocks, and one it can measure in a single request
    that holds all of it."""

    def __init__(self, file):
        self.read = file.read


_COPY_POLL_INTERVAL = 0.1


def _copy_id(props) -> "str | None":
    try:
        return props["copy_id"]
    except (KeyError, TypeError):
        pass
    return getattr(getattr(props, "copy", None), "id", None)


def _copy_status(props) -> "str | None":
    # `start_copy_from_url()` returns a dict with "copy_status";
    # `get_blob_properties()` returns BlobProperties, whose status lives at
    # `.copy.status` -- indexing it with "copy_status" raised KeyError.
    try:
        return props["copy_status"]
    except (KeyError, TypeError):
        pass
    return getattr(getattr(props, "copy", None), "status", None)


def _if_not_modified():
    """`MatchConditions.IfNotModified`, which `etag=` is taken with. Without
    the SDK there is nothing to take it, and the name stands for it."""
    try:
        from azure.core import MatchConditions
    except ImportError:
        return "IfNotModified"
    return MatchConditions.IfNotModified


class _AZSnapshot:
    """The ranged reads of one open stream, as one version of the blob. The
    first response reports the blob's ETag and every later request requires
    it, so a blob replaced between two of them answers 412 and the stream
    raises instead of returning the bytes of two blobs. Each open has its own,
    so two opens are two snapshots."""

    def __init__(self, path: "AZPath", blob_client):
        self.object_path = path
        self._client = blob_client
        self.etag = None
        self.total = None

    def _conditions(self) -> dict:
        if self.etag is None:
            return {}
        return {"etag": self.etag, "match_condition": _if_not_modified()}

    def _failure(self, error: BaseException) -> "OSError | None":
        path = self.object_path
        if http_status(error) == 412 and self.etag is not None:
            # Replaced -- or deleted, which a service may also answer 412.
            if path._has_object(path.key):
                return _failures.changed_error(path)
            return FileNotFoundError(
                _errno.ENOENT, "No such file or directory", str(path)
            )
        return oserror(error, path)

    def fetch(self, start: int, length: int) -> bytes:
        """The bytes from `start`, at most `length`; none at or past the end (a
        range there is answered 416, which is also an empty blob's answer)."""
        try:
            downloader = self._client.download_blob(
                offset=start, length=length, **self._conditions()
            )
            data = downloader.readall()
        except Exception as error:
            if http_status(error) == 416:
                return b""
            failure = self._failure(error)
            if failure is None:
                raise
            raise failure from None
        if self.etag is None:
            properties = downloader.properties
            self.etag = properties.etag
            # "bytes 0-524287/33554432": the size of the blob, not of the range.
            total = (getattr(properties, "content_range", None) or "").rpartition("/")[
                2
            ]
            self.total = int(total) if total.isdigit() else None
        return data

    def size(self) -> int:
        """The size of the version being read."""
        if self.total is not None:
            return self.total
        if self.etag is None:
            return self.object_path.stat().st_size
        try:
            return self._client.get_blob_properties(**self._conditions())["size"]
        except Exception as error:
            failure = self._failure(error)
            if failure is None:
                raise
            raise failure from None


class AZPath(ObjectStorePath):
    """A path in an Azure Blob Storage container, read and written through
    `azure.storage.blob`. Needs the `az` extra.

    `AZPath("dir/key", container="cont", account="acct")` is the blob
    `dir/key` of the container `cont` of the storage account `acct`, reached
    through `https://acct.blob.core.windows.net` with azure-identity's
    `DefaultAzureCredential` (an `ImportError` naming the extra if
    azure-identity is missing); paths of one account share one default
    backend per process. Client options (`credential=`, `account_url=`,
    `connection_string=`, ...) build the path's own `AZBackend`, `account=`
    among them or not; `backend=` shares one client between paths built
    separately, or plugs in a `BaseAZBackend` of your own. Giving `backend=`
    together with `account=` or a client option is a `TypeError`, and so is
    giving none of them (unless a segment is an `AZPath`, whose backend is
    kept). A path joined from another keeps its container and its backend, and
    `container=` moves it to another container on the same backend. Nothing
    connects when a path is built.

    Azure Blob has no real directories -- `is_dir()` is prefix emulation (any
    blob key under `"<path>/"`), and `mkdir()` creates a zero-byte
    `"<path>/"` marker blob. `rename()` uses server-side copy + delete (same
    container only) instead of the generic download+upload+delete `move()`
    fallback; a prefix directory is not renamed (NotImplementedError), so
    `move()` copies and removes it."""

    __slots__ = ()
    _TOP = "container"
    _LISTING_ERRORS = (Exception,)

    if _ty.TYPE_CHECKING:
        _backend: BaseAZBackend

    def __init__(
        self,
        *segments: "str | AZPath",
        container: "str | None" = None,
        account: "str | None" = None,
        backend: "BaseAZBackend | None" = None,
        **client_kwargs,
    ):
        text, donor = self._join(segments)
        place = check_place("container", container, donor, "AZPath")
        if backend is not None and (account is not None or client_kwargs):
            raise TypeError(
                "pass either backend= or account= and the client options "
                "(credential=, account_url=, ...), not both"
            )
        if account is not None:
            check_place("account", account, None, "AZPath")
        if backend is None:
            if client_kwargs:
                backend = AZBackend(account, **client_kwargs)
            elif account is not None:
                backend = _default_backend(account)
            elif donor is not None:
                backend = donor._backend
            else:
                raise TypeError(
                    "an AZPath needs account=, backend= or the client options"
                )
        self._initialize(text, place, backend)

    def _keywords(self) -> dict:
        return {"container": self._place}

    def _identity(self) -> tuple:
        # Two accounts can each hold a container of one name.
        return (self.account or "", self._place, self.key)

    def _backend_setting(self, name: str):
        """The `read_chunk` or `copy_timeout` of this path's backend; a backend
        that does not define it has the default of `BaseAZBackend`."""
        return getattr(self._backend, name, getattr(BaseAZBackend, name))

    def _default_keywords(self) -> dict:
        account = self.account
        if account is None:
            raise TypeError(
                f"cannot pickle an {type(self).__name__} whose backend is not "
                "picklable and names no account"
            )
        return {"account": account}

    def _uri_root(self) -> str:
        account = self.account
        if account is None:
            raise NotImplementedError("as_uri() needs a backend that names its account")
        return f"az://{account}/{self._place}"

    def __str__(self) -> str:
        try:
            return self.as_uri()
        except NotImplementedError:
            return f"az:///{self._place}/{self.key}"

    def __repr__(self) -> str:
        shown = [repr(self.as_posix()), f"container={self._place!r}"]
        backend = self._backend
        account = self.account
        if account is not None and backend is _DEFAULT_BACKENDS.get(account):
            shown.append(f"account={account!r}")
        else:
            shown.append(f"backend={backend!r}")
        return f"{type(self).__name__}({', '.join(shown)})"

    @property
    def account(self) -> "str | None":
        """The storage account, when the backend names it."""
        return getattr(self._backend, "account", None)

    @property
    def container(self) -> str:
        """The name of the container."""
        return self._place

    @property
    def _client(self):
        return self._backend.client()

    @property
    def _container(self):
        return self._client.get_container_client(self._place)

    def _properties(self, key: str):
        """The properties of the blob at `key`, or None if there is no such
        blob. Any other failure raises (as OSError when it is an error
        reply)."""
        blob_client = self._container.get_blob_client(key)
        try:
            with translate_errors(self):
                return blob_client.get_blob_properties()
        except FileNotFoundError:
            return None

    def _scandir(self):
        # Each walk_blobs call already carries size/mtime for every blob --
        # reuse it instead of `iterdir()` + a stat call per child.
        BlobPrefix = _import_client("azure.storage.blob", "az").BlobPrefix

        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        with translate_errors(self):
            # walk_blobs provides both blobs and common prefixes (directories)
            items = list(
                self._container.walk_blobs(name_starts_with=prefix, delimiter="/")
            )
        if not items and self.key:
            # Nothing under the prefix: a missing path or a file, which
            # pathlib's iterdir() refuses. Only an empty listing pays this.
            if not self.stat().is_dir():
                raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        # A key that is both a blob and a prefix (`x` and `x/y`) lists as the blob,
        # agreeing with stat()'s exact-blob precedence. The SDK returns a page's prefixes
        # before its blobs, so the blobs are collected first.
        blobs = {
            item.name[len(prefix) :]
            for item in items
            if not isinstance(item, BlobPrefix)
        }
        for item in items:
            if isinstance(item, BlobPrefix):
                name = item.name[len(prefix) :].rstrip("/")
                if name in blobs:
                    continue
                # A blob name segment is whatever its writer chose: "..", "."
                # or an embedded "/" must not become a child path.
                if _providers.is_safe_child_name(name) and name not in seen:
                    seen.add(name)
                    yield name, FileStat(is_dir=True)
                continue

            name = item.name[len(prefix) :]
            if _providers.is_safe_child_name(name) and name not in seen:
                seen.add(name)
                mtime = int(item.last_modified.timestamp()) if item.last_modified else 0
                yield name, FileStat(
                    st_size=item.size or 0, st_mtime=mtime, is_dir=False
                )

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            if not self.key:
                # A container, not a blob.
                raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
            # The client is resolved outside the translation, so a missing
            # SDK or an unusable default backend raises as itself instead of
            # FileNotFoundError.
            snapshot = _AZSnapshot(self, self._container.get_blob_client(self.key))
            try:
                first = snapshot.fetch(0, self._backend_setting("read_chunk"))
            except FileNotFoundError:
                if self.is_dir():
                    raise IsADirectoryError(
                        _errno.EISDIR, "Is a directory", str(self)
                    ) from None
                raise
            raw = RangeReader(
                snapshot.fetch,
                snapshot.size,
                first,
                self._backend_setting("read_chunk"),
            )
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                with raw:
                    return _AZWriteStream(self, initial=raw)
            if buffering == 0:
                return raw
            size = _io.DEFAULT_BUFFER_SIZE if buffering in (-1, 1) else buffering
            return _io.BufferedReader(raw, size)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        if not self.key:
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        if mode == "w" and self._holds_keys(self.key):
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        return _AZWriteStream(self, exclusive=(mode == "x"))

    def _upload_file(self, file, size: int, *, exclusive=False) -> None:
        """Upload the `size` bytes of `file` as the blob: one request while it
        fits in memory, else in blocks."""
        if size <= SPOOL_SIZE:
            self._upload(file.read(), exclusive=exclusive)
            return
        blob_client = self._container.get_blob_client(self.key)
        with translate_errors(self, create=exclusive):
            blob_client.upload_blob(_ReadOnly(file), overwrite=not exclusive)

    def _upload(self, data: bytes, *, key=None, exclusive=False) -> None:
        blob_client = self._container.get_blob_client(self.key if key is None else key)
        with translate_errors(self, create=exclusive):
            # overwrite=False sends If-None-Match: *, so a concurrent creator
            # makes this fail (409), never silently overwritten.
            blob_client.upload_blob(data, overwrite=not exclusive)

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        """`(name, size)` of every blob under `prefix`, with no delimiter."""
        with translate_errors(self):
            return [
                (blob.name, blob.size or 0)
                for blob in self._container.list_blobs(name_starts_with=prefix)
            ]

    def _delete_each(self, keys, on_error, *, ignore_missing=False):
        for key in keys:
            try:
                with translate_errors(self._key_path(key)):
                    self._container.get_blob_client(key).delete_blob()
            except Exception as error:
                if ignore_missing and isinstance(error, FileNotFoundError):
                    # Removed by the batch that was just retried.
                    continue
                if not on_error(error, self._key_path(key)):
                    raise

    @staticmethod
    def _abort_copy(blob_client, copy_id) -> None:
        """Stop a copy that is still pending; best effort."""
        if copy_id is not None:
            try:
                blob_client.abort_copy(copy_id)
            except Exception:
                pass

    def _stat_root(self) -> FileStat:
        # Ask the server, as S3Path does with HeadBucket, so a mistyped name
        # does not read as an existing directory. A one-item listing needs only
        # the permission the rest of the path uses.
        with translate_errors(self):
            for _ in self._container.list_blobs(
                name_starts_with="", results_per_page=1
            ):
                break
        return FileStat(is_dir=True)

    def _stat_object(self, key: str) -> "FileStat | None":
        # Only a not-found reply means "no such blob": a transient or
        # permission error read as "missing" let copy() replace an existing
        # blob.
        props = self._properties(key)
        if props is None:
            return None
        return FileStat(
            st_size=props["size"],
            st_mtime=(
                int(props["last_modified"].timestamp())
                if props.get("last_modified")
                else 0
            ),
            is_dir=False,
        )

    def _stored_digests(self, key: str, algorithm: str) -> "dict[str, str] | None":
        props = self._properties(key)
        if props is None:
            return None
        settings = props.get("content_settings")
        md5 = None if settings is None else settings.get("content_md5")
        return {"md5": bytes(md5).hex()} if md5 else {}

    def _has_object(self, key: str) -> bool:
        return self._properties(key) is not None

    def _has_prefix(self, key: str) -> bool:
        with translate_errors(self):
            # One item answers; the default page would carry up to 5000.
            for _ in self._container.list_blobs(
                name_starts_with=f"{key}/", results_per_page=1
            ):
                return True
        return False

    def _first_keys(self, prefix: str, limit: int) -> "list[str]":
        with translate_errors(self):
            return [
                blob.name
                for blob in _itertools.islice(
                    self._container.list_blobs(
                        name_starts_with=prefix, results_per_page=limit
                    ),
                    limit,
                )
            ]

    def _put_marker(self, key: str) -> None:
        self._upload(b"", key=key, exclusive=True)

    def _delete_object(self, key: str, *, missing_ok: bool) -> None:
        blob_client = self._container.get_blob_client(key)
        try:
            with translate_errors(self):
                blob_client.delete_blob()
        except FileNotFoundError:
            # Deleted since the caller looked. Anything else -- a lease, a
            # permission error -- is not "already gone" and raises.
            if not missing_ok:
                raise

    def _delete_keys(self, keys: "list[str]", on_error) -> None:
        try:
            delete_blobs = getattr(self._container, "delete_blobs")
        except AttributeError:
            delete_blobs = None
        if callable(delete_blobs):
            for index in range(0, len(keys), 256):
                batch = keys[index : index + 256]
                try:
                    delete_blobs(*batch)
                except Exception:
                    # A partial failure has already deleted some keys, a rejected batch (auth on the
                    # batch endpoint, an emulator without batch support) none: either way every key is
                    # retried on its own, so one failing blob does not leave the rest behind.
                    self._delete_each(batch, on_error, ignore_missing=True)
            return

        self._delete_each(keys, on_error)

    def _rename_source(self, key: str):
        return self._properties(key)

    def _copy_blob(self, dest: "AZPath", dest_key: str) -> None:
        """Copy this blob onto `dest_key` of its container with the service's
        copy, waiting for it to finish; the destination holds the content when
        it returns."""
        source_url = self._container.get_blob_client(self.key).url
        dest_blob_client = self._container.get_blob_client(dest_key)
        # start_copy_from_url is async, poll for completion
        copy_props = dest_blob_client.start_copy_from_url(source_url)
        copy_id = _copy_id(copy_props)
        # Poll until copy is complete
        timeout = self._backend_setting("copy_timeout")
        deadline = _time.monotonic() + timeout
        while _copy_status(copy_props) == "pending":
            if _time.monotonic() >= deadline:
                self._abort_copy(dest_blob_client, copy_id)
                raise TimeoutError(
                    _errno.ETIMEDOUT,
                    f"the copy did not finish within {timeout:g} s",
                    str(self),
                )
            _time.sleep(_COPY_POLL_INTERVAL)
            dest_blob_client = self._container.get_blob_client(dest_key)
            copy_props = dest_blob_client.get_blob_properties()
        if _copy_status(copy_props) != "success":
            raise OSError(f"Copy failed: {self} -> {dest}")

    def _rename_copy(self, props, dest: "AZPath", dest_key: str) -> None:
        with translate_errors(self):
            self._copy_blob(dest, dest_key)
            self._container.get_blob_client(self.key).delete_blob()

    def _store_copy_possible(self, target) -> bool:
        # The service copies within one container, as rename() does.
        return super()._store_copy_possible(target) and self._place == target._place

    def _copy_object(self, target: "AZPath", size: int) -> bool:
        try:
            self._copy_blob(target, target.key)
        except Exception as error:
            if http_status(error) == 501:
                return False
            translated = oserror(error, self)
            if translated is None:
                raise
            raise translated from None
        return True
