from __future__ import annotations

import errno as _errno
import io as _io
import typing as _ty

from pathlib_next import FileStat
from pathlib_next import providers as _providers

from . import _failures
from ._gs_backend import UNSET, BaseGSBackend, GSBackend, _default_backend
from ._gs_errors import BatchReplyError, http_status, oserror, translate_errors
from . import _reader
from ._reader import RangeReader
from ._storepath import ObjectStorePath, check_place, digest_hex
from ._writer import SPOOL_SIZE, SpoolUploadStream

_DELETE_BATCH = 100
"""Deletes sent in one batch request (the API takes up to 1000)."""

_GSWriteStream = SpoolUploadStream

_UPLOAD_CHUNK = 2 * 1024**2
"""Bytes of one request of a resumable upload (a multiple of 256 KiB)."""


class _GSSnapshot:
    """The ranged reads of one open stream, as one version of the object. The
    first response reports the object's generation and every later request
    requires it (`ifGenerationMatch`), so an object replaced between two of
    them answers 412 and the stream raises instead of returning the bytes of
    two objects. Each open has its own, so two opens are two snapshots."""

    def __init__(self, path: "GSPath"):
        self.object_path = path
        self.generation = None

    def _failure(self, error: BaseException) -> "OSError | None":
        path = self.object_path
        if http_status(error) == 412 and self.generation is not None:
            # Replaced -- or deleted, which a store may also answer 412.
            if path._has_object(path.key):
                return _failures.changed_error(path)
            return FileNotFoundError(
                _errno.ENOENT, "No such file or directory", str(path)
            )
        return oserror(error, path)

    def fetch(self, start: int, length: int) -> bytes:
        """The bytes from `start`, at most `length`; none at or past the end (a
        range there is answered 416, which is also an empty object's answer)."""
        path = self.object_path
        # The client is resolved outside the translation, so a missing SDK
        # raises ImportError instead of FileNotFoundError.
        blob = path._bucket.blob(path.key)
        pinned = (
            {} if self.generation is None else {"if_generation_match": self.generation}
        )
        try:
            # No checksum: the SDK checks a download against the CRC32C or MD5
            # of the whole object, which a part of it cannot match. The
            # generation is what ties each range to the object the first read.
            data = blob.download_as_bytes(
                start=start,
                end=start + length - 1,
                checksum=None,
                **pinned,
                **path._options,
            )
        except Exception as error:
            if http_status(error) == 416:
                return b""
            failure = self._failure(error)
            if failure is None:
                raise
            raise failure from None
        if self.generation is None:
            self.generation = blob.generation
        return data

    def size(self) -> int:
        """The size of the version being read."""
        path = self.object_path
        if self.generation is None:
            return path.stat().st_size
        blob = path._bucket.blob(path.key)
        try:
            blob.reload(if_generation_match=self.generation, **path._options)
        except Exception as error:
            failure = self._failure(error)
            if failure is None:
                raise
            raise failure from None
        return blob.size


class GSPath(ObjectStorePath):
    """A path in a Google Cloud Storage bucket, read and written through
    `google.cloud.storage`. Needs the `gs` extra.

    `GSPath("dir/key", bucket="bkt")` is the key `dir/key` of the bucket
    `bkt`. The client options (`project=`, `credentials=`,
    `client_options=`, ...) and `timeout=` / `retry=` build the path's own
    `GSBackend`; `backend=` shares one client between paths built separately,
    or plugs in a `BaseGSBackend` of your own. Giving `backend=` together with
    a client option is a `TypeError`. Without either, paths share one default
    backend per process. A path joined from another keeps its bucket and its
    backend, and `bucket=` moves it to another bucket on the same backend.
    Nothing connects when a path is built.

    GCS has no real directories -- `is_dir()` is prefix emulation (any object
    key under `"<path>/"`), and `mkdir()` creates a zero-byte `"<path>/"`
    marker object. `rename()` uses server-side copy + delete (same bucket
    only) instead of the generic download+upload+delete `move()` fallback; a
    prefix directory is not renamed (NotImplementedError), so `move()` copies
    and removes it."""

    __slots__ = ()
    _LISTING_ERRORS = (Exception,)
    _CHECKSUMS = frozenset({"md5", "crc32c"})

    if _ty.TYPE_CHECKING:
        _backend: BaseGSBackend

    def __init__(
        self,
        *segments: "str | GSPath",
        bucket: "str | None" = None,
        backend: "BaseGSBackend | None" = None,
        timeout=UNSET,
        retry=UNSET,
        **client_kwargs,
    ):
        text, donor = self._join(segments)
        place = check_place("bucket", bucket, donor, "GSPath")
        options = {
            name: value
            for name, value in (("timeout", timeout), ("retry", retry))
            if value is not UNSET
        }
        options.update(client_kwargs)
        if backend is not None and options:
            raise TypeError(
                "pass either backend= or the client options "
                "(project=, credentials=, timeout=, ...), not both"
            )
        if backend is None:
            if options:
                backend = GSBackend(**options)
            elif donor is not None:
                backend = donor._backend
            else:
                backend = _default_backend()
        self._initialize(text, place, backend)

    def _keywords(self) -> dict:
        return {"bucket": self._place}

    def _uri_root(self) -> str:
        return f"gs://{self._place}"

    def __repr__(self) -> str:
        shown = [repr(self.as_posix()), f"bucket={self._place!r}"]
        if self._backend is not _default_backend():
            shown.append(f"backend={self._backend!r}")
        return f"{type(self).__name__}({', '.join(shown)})"

    @property
    def bucket(self) -> str:
        """The name of the bucket."""
        return self._place

    @property
    def _client(self):
        return self._backend.client()

    @property
    def _bucket(self):
        return self._client.bucket(self._place)

    @property
    def _options(self) -> dict:
        """The backend's `timeout=`/`retry=` for an SDK call."""
        options = getattr(self._backend, "call_options", None)
        return options() if callable(options) else {}

    def _reload(self, key: str):
        """The reloaded blob at `key`, or None if there is no such object.
        Any other failure raises (as OSError when it is an API error)."""
        blob = self._bucket.blob(key)
        try:
            with translate_errors(self):
                blob.reload(**self._options)
        except FileNotFoundError:
            return None
        return blob

    def _scandir(self):
        # Each list_blobs call already carries size/mtime for every object --
        # reuse it instead of `iterdir()` + a stat call per child.
        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        with translate_errors(self):
            iterator = self._bucket.list_blobs(
                prefix=prefix, delimiter="/", **self._options
            )
            blobs = list(iterator)
            prefixes = list(iterator.prefixes)
        if not blobs and not prefixes and self.key:
            # Nothing under the prefix: a missing path or a file, which
            # pathlib's iterdir() refuses. Only an empty listing pays this.
            if not self.stat().is_dir():
                raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        # A key that is both an object and a prefix (`x` and `x/y`) lists as
        # the object, agreeing with stat()'s exact-object precedence; the
        # subtree under it is not listed.
        objects = {blob.name[len(prefix) :] for blob in blobs}
        # Common prefixes (directories)
        for common_prefix in prefixes:
            name = common_prefix[len(prefix) :].rstrip("/")
            if name in objects:
                continue
            # A key segment is whatever its writer chose: "..", "." or an
            # embedded "/" must not become a child path.
            if _providers.is_safe_child_name(name) and name not in seen:
                seen.add(name)
                yield name, FileStat(is_dir=True)
        # Blobs (files)
        for blob in blobs:
            name = blob.name[len(prefix) :]
            if name.endswith("/"):
                continue
            if _providers.is_safe_child_name(name) and name not in seen:
                seen.add(name)
                mtime = int(blob.updated.timestamp()) if blob.updated else 0
                yield name, FileStat(
                    st_size=blob.size or 0, st_mtime=mtime, is_dir=False
                )

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            if not self.key:
                raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
            snapshot = _GSSnapshot(self)
            try:
                first = snapshot.fetch(0, _reader.READ_CHUNK)
            except FileNotFoundError:
                if self.is_dir():
                    raise IsADirectoryError(
                        _errno.EISDIR, "Is a directory", str(self)
                    ) from None
                raise
            raw = RangeReader(snapshot.fetch, snapshot.size, first)
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                with raw:
                    return _GSWriteStream(self, initial=raw)
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
        return _GSWriteStream(self, exclusive=(mode == "x"))

    def _upload_file(self, file, size: int, *, exclusive=False) -> None:
        """Upload the `size` bytes of `file` as the object: one request while it
        fits in memory, else a resumable upload in `_UPLOAD_CHUNK` requests."""
        if size <= SPOOL_SIZE:
            self._upload(file.read(), exclusive=exclusive)
            return
        blob = self._bucket.blob(self.key, chunk_size=_UPLOAD_CHUNK)
        # Generation 0 matches only a missing object, as in _upload().
        conditions = {"if_generation_match": 0} if exclusive else {}
        with translate_errors(self, create=exclusive):
            blob.upload_from_file(file, size=size, **conditions, **self._options)

    def _upload(self, data: bytes, *, key=None, exclusive=False) -> None:
        blob = self._bucket.blob(self.key if key is None else key)
        with translate_errors(self, create=exclusive):
            if exclusive:
                # Generation 0 matches only a missing object: a concurrent
                # creator makes this fail with 412, never overwritten.
                blob.upload_from_string(data, if_generation_match=0, **self._options)
            else:
                blob.upload_from_string(data, **self._options)

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        """`(name, size)` of every object under `prefix`, with no delimiter."""
        with translate_errors(self):
            return [
                (blob.name, blob.size or 0)
                for blob in self._bucket.list_blobs(prefix=prefix, **self._options)
            ]

    def _stat_root(self) -> FileStat:
        # The bucket root: ask the server, as s3: does with HeadBucket, so a mistyped bucket
        # does not read as an existing directory. A one-item listing needs only the
        # object-list permission the rest of the path uses.
        with translate_errors(self):
            for _ in self._bucket.list_blobs(max_results=1, **self._options):
                break
        return FileStat(is_dir=True)

    def _stat_object(self, key: str) -> "FileStat | None":
        # Only a not-found reply means "no such object": a transient or
        # permission error read as "missing" let copy() replace an existing
        # object.
        blob = self._reload(key)
        if blob is None:
            return None
        return FileStat(
            st_size=blob.size,
            st_mtime=int(blob.updated.timestamp()) if blob.updated else 0,
            is_dir=False,
        )

    def _stored_digests(self, key: str, algorithm: str) -> "dict[str, str] | None":
        blob = self._reload(key)
        if blob is None:
            return None
        digests = {}
        # An object stored with a content encoding is read back decoded, so
        # the digests of the stored bytes are not those of what a read gives.
        if getattr(blob, "content_encoding", None) in (None, "", "identity"):
            for name, value in (("md5", blob.md5_hash), ("crc32c", blob.crc32c)):
                if value:
                    digests[name] = digest_hex(value)
        return digests

    def _has_object(self, key: str) -> bool:
        return self._reload(key) is not None

    def _has_prefix(self, key: str) -> bool:
        with translate_errors(self):
            for _ in self._bucket.list_blobs(
                prefix=f"{key}/", max_results=1, **self._options
            ):
                return True
        return False

    def _first_keys(self, prefix: str, limit: int) -> "list[str]":
        with translate_errors(self):
            return [
                blob.name
                for blob in self._bucket.list_blobs(
                    prefix=prefix, max_results=limit, **self._options
                )
            ]

    def _put_marker(self, key: str) -> None:
        self._upload(b"", key=key, exclusive=True)

    def _delete_object(self, key: str, *, missing_ok: bool) -> None:
        blob = self._bucket.blob(key)
        try:
            with translate_errors(self):
                blob.delete(**self._options)
        except FileNotFoundError:
            # Deleted since the caller looked. Anything else -- a hold, a
            # permission error -- is not "already gone" and raises.
            if not missing_ok:
                raise

    def _delete_each(self, keys, on_error, *, ignore_missing=False) -> None:
        for key in keys:
            try:
                with translate_errors(self._key_path(key)):
                    self._bucket.blob(key).delete(**self._options)
            except Exception as error:
                if ignore_missing and isinstance(error, FileNotFoundError):
                    # Removed by the batch that was just retried.
                    continue
                if not on_error(error, self._key_path(key)):
                    raise

    def _delete_batch(self, keys: "list[str]"):
        """Delete `keys` in one batch request and return its replies, one per
        key in order. None when the batch cannot say what it did to each key:
        the client has no batches, the request failed, or the replies do not
        match what was sent."""
        make_batch = getattr(self._client, "batch", None)
        if not callable(make_batch):
            return None
        bucket = self._bucket
        try:
            with make_batch(raise_exception=False) as batch:
                for key in keys:
                    bucket.blob(key).delete(**self._options)
        except Exception:
            return None
        replies = getattr(batch, "_responses", None)
        if isinstance(replies, list) and len(replies) == len(keys):
            return replies
        return None

    def _delete_keys(self, keys: "list[str]", on_error) -> None:
        for index in range(0, len(keys), _DELETE_BATCH):
            chunk = keys[index : index + _DELETE_BATCH]
            replies = self._delete_batch(chunk)
            if replies is None:
                # A rejected batch deleted none and a broken one an unknown
                # number: each key is deleted on its own, so one failing blob
                # leaves no other behind.
                self._delete_each(chunk, on_error, ignore_missing=True)
                continue
            for key, reply in zip(chunk, replies):
                status = reply.status_code
                # A blob gone since the listing is what the delete is for.
                if 200 <= status < 300 or status == 404:
                    continue
                where = self._key_path(key)
                failed = oserror(BatchReplyError(status, reply), where)
                if not on_error(failed, where):
                    raise failed

    def _copy_object(self, target: "GSPath", size: int) -> bool:
        # A target with no bucket is "a path needs a bucket", as for a write.
        destination = target._bucket
        try:
            self._bucket.copy_blob(
                self._bucket.blob(self.key), destination, target.key, **self._options
            )
        except Exception as error:
            if http_status(error) == 501:
                return False
            translated = oserror(error, self)
            if translated is None:
                raise
            raise translated from None
        return True

    def _rename_source(self, key: str):
        return self._reload(key)

    def _rename_copy(self, source_blob, dest, dest_key: str) -> None:
        with translate_errors(self):
            self._bucket.copy_blob(source_blob, self._bucket, dest_key, **self._options)
            source_blob.delete(**self._options)
