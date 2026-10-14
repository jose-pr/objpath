from __future__ import annotations

import errno as _errno
import io as _io
import itertools as _itertools
import re as _re
import typing as _ty

from pathlib_next import FileStat
from pathlib_next import providers as _providers

from ._s3_backend import BaseS3Backend, S3Backend, _default_backend
from ._s3_errors import (
    S3_ERRORS,
    TRANSPORT_ERRORS,
    S3UploadFailedError,
    botoexc,
    is_not_found,
    is_not_implemented,
    is_precondition_failed,
    oserror,
    reply_oserror,
)
from ._digests import digest_hex
from ._places import check_place
from ._storepath import ObjectStorePath
from ._writer import SpoolUploadStream

_PUT_OBJECT_LIMIT = 5 * 1024**3
"""Largest body a single PutObject accepts, and the largest source a single
CopyObject copies; above it writes and renames use boto3's managed
(multipart) transfers."""

_SPOOL_SIZE = 8 * 1024**2
"""Bytes a write stream keeps in memory before spilling to a temp file."""

_PLAIN_MD5 = _re.compile("[0-9a-fA-F]{32}")

#: HeadObject fields that hold a checksum of the whole object, by the
#: `hashlib`-style name `checksum()` takes.
_CHECKSUM_FIELDS = {
    "crc32": "ChecksumCRC32",
    "crc32c": "ChecksumCRC32C",
    "crc64nvme": "ChecksumCRC64NVME",
    "sha1": "ChecksumSHA1",
    "sha256": "ChecksumSHA256",
}


def _plain_md5(head) -> "str | None":
    """The MD5 of the content that a HeadObject reply's ETag is, or None.
    S3 makes the ETag the MD5 only for an object that a single request wrote
    and that is not encrypted with KMS or a customer key; a multipart
    object's ETag ends in `-<parts>`."""
    etag = str(head.get("ETag", "")).strip('"')
    if not _PLAIN_MD5.fullmatch(etag):
        return None
    if head.get("ServerSideEncryption") not in (None, "AES256"):
        return None
    if head.get("SSECustomerAlgorithm") or head.get("SSEKMSKeyId"):
        return None
    return etag.lower()


def _copy_call(client, source: dict, bucket: str, key: str, size: int, extra: dict):
    """One server-side copy of `source` onto `bucket`/`key`: CopyObject, or
    boto3's managed copy (UploadPartCopy) for a source CopyObject refuses."""
    managed_copy = getattr(client, "copy", None)
    if size > _PUT_OBJECT_LIMIT and callable(managed_copy):
        managed_copy(source, bucket, key, ExtraArgs=extra or None)
    else:
        client.copy_object(Bucket=bucket, Key=key, CopySource=source, **extra)


class _S3BodyReader(_io.RawIOBase):
    """Reads a GetObject `StreamingBody` as it arrives, instead of loading
    the whole object into memory first. A failure of the connection in the
    middle of the body is the `OSError` `oserror()` makes of it."""

    def __init__(self, body, path):
        super().__init__()
        self._body = body
        self._path = path

    def readable(self):
        return True

    def readinto(self, buffer):
        try:
            data = self._body.read(len(buffer))
        except TRANSPORT_ERRORS as error:
            raise oserror(error, self._path) from None
        size = len(data)
        buffer[:size] = data
        return size

    def close(self):
        if self.closed:
            return
        try:
            self._body.close()
        finally:
            super().close()


class _S3WriteStream(SpoolUploadStream):
    """The spooled write of `S3Path`, uploaded on close() as a managed
    transfer, multipart above boto3's threshold, so objects over PutObject's
    5 GB limit can be written.

    `exclusive` (`open("x")`) creates the key with a conditional PutObject
    (`IfNoneMatch="*"`), atomic against a concurrent creator."""

    spool_size = _SPOOL_SIZE

    def _upload(self):
        path = self._path
        client = path._client
        body = self._file
        body.seek(0, _io.SEEK_END)
        size = body.tell()
        body.seek(0)
        if self._exclusive:
            if size <= _PUT_OBJECT_LIMIT and path._put_exclusive(body):
                return
            # Over PutObject's limit (managed uploads take no IfNoneMatch),
            # or a store without conditional writes: only this check guards
            # the key.
            if path.exists():
                raise FileExistsError(_errno.EEXIST, "File exists", str(path))
            body.seek(0)
        try:
            upload_fileobj = getattr(client, "upload_fileobj", None)
            if upload_fileobj is None:
                # A plain botocore client (no boto3 transfer methods).
                client.put_object(Bucket=path.bucket, Key=path.key, Body=body)
            else:
                upload_fileobj(body, path.bucket, path.key)
        except S3_ERRORS as error:
            raise oserror(error, path) from None
        except S3UploadFailedError as error:
            # boto3 wraps the store's answer; the answer is what means something.
            cause = error.__context__
            if isinstance(cause, S3_ERRORS):
                raise oserror(cause, path) from None
            raise OSError(_errno.EIO, str(error), str(path)) from None


class S3Path(ObjectStorePath):
    """A path in an Amazon S3 bucket (or any S3-compatible store), read and
    written through `boto3`. Needs the `s3` extra.

    `S3Path("dir/key", bucket="bkt")` is the key `dir/key` of the bucket
    `bkt`. The client options (`region_name=`, `endpoint_url=`,
    `aws_access_key_id=`, ...) build the path's own `S3Backend`; `backend=`
    shares one client between paths built separately, or plugs in a
    `BaseS3Backend` of your own. Giving `backend=` together with a client
    option is a `TypeError`. Without either, paths share one default backend
    per process. A path joined from another keeps its bucket and its backend,
    and `bucket=` moves it to another bucket on the same backend. Nothing
    connects when a path is built.

    S3 has no real directories -- `is_dir()` is prefix emulation (any object
    key under `"<path>/"`), and `mkdir()` creates a zero-byte `"<path>/"`
    marker object (the same convention the AWS console itself uses for an
    empty "folder"). `rename()` uses server-side `copy_object` +
    `delete_object` (same bucket only) instead of the generic
    download+upload+delete `move()` fallback; a prefix directory is not
    renamed (NotImplementedError), so `move()` copies and removes it."""

    __slots__ = ()

    if _ty.TYPE_CHECKING:
        _backend: BaseS3Backend

    def __init__(
        self,
        *segments: "str | S3Path",
        bucket: "str | None" = None,
        backend: "BaseS3Backend | None" = None,
        **client_kwargs,
    ):
        text, donor = self._join(segments)
        place = check_place("bucket", bucket, donor, "S3Path")
        if backend is not None and client_kwargs:
            raise TypeError(
                "pass either backend= or the client options "
                "(region_name=, endpoint_url=, ...), not both"
            )
        if backend is None:
            if client_kwargs:
                backend = S3Backend(**client_kwargs)
            elif donor is not None:
                backend = donor._backend
            else:
                backend = _default_backend()
        self._initialize(text, place, backend)

    def _keywords(self) -> dict:
        return {"bucket": self._place}

    def _uri_root(self) -> str:
        return f"s3://{self._place}"

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

    def _not_a_directory(self):
        return NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))

    def _scandir(self):
        # Each list_objects_v2 page already carries size/mtime for every
        # object -- reuse it instead of `iterdir()` + a HEAD per child.
        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        empty = True
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(
                Bucket=self.bucket, Prefix=prefix, Delimiter="/"
            ):
                # A key that is both an object and a prefix (`x` and `x/y`) lists as the object,
                # agreeing with stat()'s exact-object precedence. Keys sort `x` before `x/`, so an
                # earlier page never holds the prefix.
                objects = {
                    obj["Key"][len(prefix) :] for obj in page.get("Contents", [])
                }
                for common in page.get("CommonPrefixes", []):
                    empty = False
                    name = common["Prefix"][len(prefix) :].rstrip("/")
                    if name in objects:
                        continue
                    # A key segment is whatever its writer chose: "..", "." or
                    # an embedded "/" must not become a child path.
                    if _providers.is_safe_child_name(name) and name not in seen:
                        seen.add(name)
                        yield name, FileStat(is_dir=True)
                for obj in page.get("Contents", []):
                    empty = False
                    name = obj["Key"][len(prefix) :]
                    if _providers.is_safe_child_name(name) and name not in seen:
                        seen.add(name)
                        lm = obj.get("LastModified")
                        mtime = int(lm.timestamp()) if lm else 0
                        yield name, FileStat(
                            st_size=obj.get("Size", 0) or 0,
                            st_mtime=mtime,
                            is_dir=False,
                        )
        except S3_ERRORS as error:
            raise oserror(error, self) from None
        if empty and self.key:
            # Nothing under the prefix: a missing path or a file, which
            # pathlib's iterdir() refuses. Only an empty listing pays this.
            if not self.stat().is_dir():
                raise self._not_a_directory()

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            if not self.key:
                raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
            try:
                resp = self._client.get_object(Bucket=self.bucket, Key=self.key)
            except S3_ERRORS as error:
                translated = oserror(error, self)
                if isinstance(translated, FileNotFoundError) and self.is_dir():
                    translated = IsADirectoryError(
                        _errno.EISDIR, "Is a directory", str(self)
                    )
                raise translated from None
            raw = _S3BodyReader(resp["Body"], self)
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                with raw:
                    return _S3WriteStream(self, initial=raw)
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
        return _S3WriteStream(self, exclusive=(mode == "x"))

    def _put_exclusive(self, body, key=None) -> bool:
        """PutObject that creates `key` (default: this path's) only if it
        does not exist. FileExistsError if it does; False if the store does
        not implement conditional writes, so nothing was written."""
        key = self.key if key is None else key
        try:
            self._client.put_object(
                Bucket=self.bucket, Key=key, Body=body, IfNoneMatch="*"
            )
        except botoexc.ClientError as error:
            if is_precondition_failed(error):
                raise FileExistsError(_errno.EEXIST, "File exists", str(self)) from None
            if is_not_implemented(error):
                return False
            raise oserror(error, self) from None
        except TRANSPORT_ERRORS as error:
            raise oserror(error, self) from None
        return True

    def _stat_root(self) -> FileStat:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except S3_ERRORS as error:
            raise oserror(error, self) from None
        return FileStat(is_dir=True)

    def _stat_object(self, key: str) -> "FileStat | None":
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except botoexc.ClientError as error:
            # A 403 stays PermissionError: S3 answers HEAD of a missing key
            # with 403 when the caller may not list the bucket.
            if not is_not_found(error):
                raise oserror(error, self) from None
            return None
        except TRANSPORT_ERRORS as error:
            raise oserror(error, self) from None
        return FileStat(
            st_size=head["ContentLength"],
            st_mtime=int(head["LastModified"].timestamp()),
            is_dir=False,
        )

    def _stored_digests(self, key: str, algorithm: str) -> "dict[str, str] | None":
        extra = {} if algorithm == "md5" else {"ChecksumMode": "ENABLED"}
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key, **extra)
        except botoexc.ClientError as error:
            if is_not_found(error):
                return None
            raise oserror(error, self) from None
        except TRANSPORT_ERRORS as error:
            raise oserror(error, self) from None
        digests = {}
        md5 = _plain_md5(head)
        if md5 is not None:
            digests["md5"] = md5
        # A composite checksum (of a multipart object's parts) ends in
        # `-<parts>` and is never the digest of the content.
        if head.get("ChecksumType") != "COMPOSITE":
            for name, field in _CHECKSUM_FIELDS.items():
                value = head.get(field)
                if value and "-" not in value:
                    digests[name] = digest_hex(value)
        return digests

    def _has_object(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except S3_ERRORS as error:
            if isinstance(error, botoexc.ClientError) and is_not_found(error):
                return False
            raise oserror(error, self) from None
        return True

    def _has_prefix(self, key: str) -> bool:
        try:
            resp = self._client.list_objects_v2(
                Bucket=self.bucket, Prefix=f"{key}/", MaxKeys=1
            )
        except S3_ERRORS as error:
            raise oserror(error, self) from None
        return resp.get("KeyCount", 0) > 0

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        """`(key, size)` of every object under `prefix`, with no delimiter."""
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            return [
                (obj["Key"], obj.get("Size", 0) or 0)
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])
            ]
        except S3_ERRORS as error:
            raise oserror(error, self) from None

    def _first_keys(self, prefix: str, limit: int) -> "list[str]":
        try:
            resp = self._client.list_objects_v2(
                Bucket=self.bucket, Prefix=prefix, MaxKeys=limit
            )
        except S3_ERRORS as error:
            raise oserror(error, self) from None
        return [obj["Key"] for obj in resp.get("Contents", [])]

    def _put_marker(self, key: str) -> None:
        if not self._put_exclusive(b"", key=key):
            try:
                self._client.put_object(Bucket=self.bucket, Key=key, Body=b"")
            except S3_ERRORS as error:
                raise oserror(error, self) from None

    def _delete_object(self, key: str, *, missing_ok: bool) -> None:
        # S3 never answers a delete with "not found".
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except S3_ERRORS as error:
            raise oserror(error, self) from None

    def _delete_keys(self, keys: "list[str]", on_error) -> None:
        iterator = iter(keys)
        while True:
            batch = list(_itertools.islice(iterator, 1000))
            if not batch:
                break
            try:
                response = self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": key} for key in batch]},
                )
            except S3_ERRORS as error:
                translated = oserror(error, self)
                if not on_error(translated):
                    raise translated from None
                continue
            # The store refuses single keys of a batch that it accepts: each
            # is offered with its own path.
            for entry in response.get("Errors", []) if response else []:
                failed_path = self._key_path(entry.get("Key", ""))
                failed = reply_oserror(
                    str(entry.get("Code", "")),
                    None,
                    entry.get("Message", ""),
                    failed_path,
                    action="delete_objects",
                )
                if not on_error(failed, failed_path):
                    raise failed

    def _rename_source(self, key: str):
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except S3_ERRORS as error:
            if isinstance(error, botoexc.ClientError) and is_not_found(error):
                return None
            raise oserror(error, self) from None

    def _rename_copy(self, head, dest, dest_key: str) -> None:
        client = self._client
        extra = {}
        storage_class = head.get("StorageClass")
        if storage_class and storage_class != "STANDARD":
            # CopyObject writes STANDARD unless told otherwise.
            extra["StorageClass"] = storage_class
        source = {"Bucket": self.bucket, "Key": self.key}
        try:
            _copy_call(
                client,
                source,
                self.bucket,
                dest_key,
                head.get("ContentLength", 0),
                extra,
            )
            client.delete_object(Bucket=self.bucket, Key=self.key)
        except S3_ERRORS as error:
            raise oserror(error, self) from None

    def _copy_object(self, target: "S3Path", size: int) -> bool:
        client = target._client
        source = {"Bucket": self.bucket, "Key": self.key}
        try:
            _copy_call(client, source, target.bucket, target.key, size, {})
        except S3_ERRORS as error:
            if isinstance(error, botoexc.ClientError) and is_not_implemented(error):
                return False
            raise oserror(error, self) from None
        return True
