"""The fakes of the SDK client that more than one test module uses."""

from __future__ import annotations

import datetime
import warnings

import objstore_fakes
from objpath import BaseGSBackend

with warnings.catch_warnings():
    # `google-crc32c` warns on import where it has no compiled extension, and
    # the fake blobs would first import it under the filters of a test.
    warnings.simplefilter("ignore")
    try:
        import google_crc32c  # noqa: F401
    except ImportError:
        pass


class _ApiError(Exception):
    """Mirrors `google.api_core.exceptions.GoogleAPICallError`: the HTTP
    status is the `code` attribute (`NotFound.code == 404`), which is what
    `GSPath` classifies by."""

    code = None


class _Missing(_ApiError):
    code = 404


class _Forbidden(_ApiError):
    code = 403


class _PreconditionFailed(_ApiError):
    code = 412


class _ServiceUnavailable(_ApiError):
    code = 503


class _RangeNotSatisfiable(_ApiError):
    code = 416


class _FakeBlob:
    def __init__(self, bucket, name):
        self._bucket = bucket
        self.name = name
        self._generation = None

    @property
    def generation(self):
        # Known once a download or a reload has reported it, as the SDK's.
        return self._generation

    def _require(self, if_generation_match):
        """The object's current generation, after the preconditions: an object
        that is not the generation asked for is 412, and so is a missing one
        when the bucket answers a precondition before it looks for the object."""
        bucket = self._bucket
        if self.name not in bucket.objects:
            if if_generation_match is not None and bucket.missing_is_412:
                raise _PreconditionFailed(self.name)
            raise _Missing(self.name)
        current = bucket.objects.version(self.name)
        if if_generation_match is not None and if_generation_match != current:
            raise _PreconditionFailed(self.name)
        return current

    @property
    def size(self):
        return len(self._bucket.objects[self.name])

    @property
    def updated(self):
        return datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def _stored(self, field, default):
        return self._bucket.stored.get(self.name, {}).get(field, default)

    @property
    def md5_hash(self):
        return self._stored("md5_hash", objstore_fakes.md5_base64(self._content()))

    @property
    def crc32c(self):
        return self._stored("crc32c", objstore_fakes.crc32c_base64(self._content()))

    @property
    def content_encoding(self):
        return self._stored("content_encoding", None)

    def _content(self):
        return self._bucket.objects[self.name]

    def reload(self, if_generation_match=None, **_kwargs):
        if self.name in self._bucket.reload_errors:
            raise self._bucket.reload_errors[self.name]
        self._generation = self._require(if_generation_match)

    def delete(self):
        if self._bucket.batch is not None:
            self._bucket.batch.queued.append(self.name)
            return
        if self.name in self._bucket.delete_errors:
            raise OSError(self.name)
        if self.name in self._bucket.delete_raises:
            raise self._bucket.delete_raises[self.name]
        if self.name not in self._bucket.objects:
            raise _Missing(self.name)
        del self._bucket.objects[self.name]
        self._bucket.deleted.append(self.name)

    def download_as_bytes(
        self, start=None, end=None, checksum=None, if_generation_match=None, **_kwargs
    ):
        if self._bucket.before_read is not None:
            self._bucket.before_read(self.name)
        self._generation = self._require(if_generation_match)
        self._bucket.reads.append((self.name, start, if_generation_match))
        content = self._bucket.objects[self.name]
        if start is not None:
            # A range at or past the end is answered 416, an empty object's
            # included.
            if start >= len(content):
                raise _RangeNotSatisfiable(self.name)
            content = content[start : None if end is None else end + 1]
            self._bucket.ranges.append((self.name, start, end))
        self._bucket.downloads.append(self.name)
        return content

    def upload_from_file(
        self, file_obj, size=None, if_generation_match=None, **_kwargs
    ):
        self._bucket.file_uploads.append((self.name, size))
        self.upload_from_string(file_obj.read(), if_generation_match)

    def upload_from_string(self, data, if_generation_match=None):
        if self._bucket.upload_errors:
            raise self._bucket.upload_errors.pop(0)
        if if_generation_match == 0 and self.name in self._bucket.objects:
            raise _PreconditionFailed(self.name)
        self._bucket.objects[self.name] = bytes(data)
        self._bucket.uploads.append(self.name)


class _FakeIterator:
    """`list_blobs()`'s HTTPIterator: blobs when iterated, and with a
    delimiter the common `prefixes` seen while paging."""

    def __init__(self, bucket, prefix, delimiter):
        self._bucket = bucket
        self._prefix = prefix
        self._delimiter = delimiter
        self.prefixes = set()

    def __iter__(self):
        for name in sorted(self._bucket.objects):
            if not name.startswith(self._prefix):
                continue
            rest = name[len(self._prefix) :]
            if self._delimiter and self._delimiter in rest:
                self.prefixes.add(
                    self._prefix + rest.split(self._delimiter, 1)[0] + self._delimiter
                )
                continue
            yield _FakeBlob(self._bucket, name)


class _Reply:
    """One part of a batch reply: what `requests.Response` gives the SDK's
    `Batch` for it."""

    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content


class _FakeBatch:
    """The SDK's `Batch`: inside the block `blob.delete()` only queues; on
    exit one request answers every queued delete, and `_responses` holds a
    reply per delete in order."""

    def __init__(self, bucket, raise_exception=True):
        self._bucket = bucket
        self.queued = []
        self._responses = []

    def __enter__(self):
        self._bucket.batch = self
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._bucket.batch = None
        if exc_type is None:
            self._finish()

    def _finish(self):
        bucket = self._bucket
        bucket.batch_calls.append(list(self.queued))
        if bucket.batch_rejected:
            raise _ServiceUnavailable("503 batch")
        for name in self.queued:
            if name in bucket.batch_statuses:
                status = bucket.batch_statuses[name]
            elif name in bucket.objects:
                del bucket.objects[name]
                bucket.deleted.append(name)
                status = 204
            else:
                status = 404
            self._responses.append(_Reply(status, b'{"error": {"message": "refused"}}'))
        if bucket.batch_short:
            del self._responses[-1]


class _FakeBucket:
    def __init__(self):
        self.objects = objstore_fakes.VersionedObjects()
        self.before_read = None
        self.missing_is_412 = False
        self.reads = []
        self.downloads = []
        self.ranges = []
        self.file_uploads = []
        self.copies = []
        self.copy_errors = {}
        self.stored = {}
        self.batch = None
        self.batch_calls = []
        self.batch_statuses = {}
        self.batch_rejected = False
        self.batch_short = False
        self.deleted = []
        self.uploads = []
        self.delete_errors = set()
        self.delete_raises = {}
        self.reload_errors = {}
        self.upload_errors = []
        self.list_calls = []

    def blob(self, name, chunk_size=None):
        return _FakeBlob(self, name)

    def list_blobs(self, prefix="", delimiter=None, **_kwargs):
        self.list_calls.append(prefix)
        return _FakeIterator(self, prefix, delimiter)

    def copy_blob(self, blob, destination_bucket, new_name):
        if blob.name in self.copy_errors:
            raise self.copy_errors[blob.name]
        if blob.name not in self.objects:
            raise _Missing(blob.name)
        destination_bucket.objects[new_name] = self.objects[blob.name]
        self.copies.append((blob.name, new_name))


class _FakeClient:
    def __init__(self, batching=False):
        self.bucket_obj = _FakeBucket()
        if batching:
            # A client that cannot batch has no such method.
            self.batch = self._batch

    def bucket(self, _name):
        return self.bucket_obj

    def _batch(self, raise_exception=True):
        return _FakeBatch(self.bucket_obj, raise_exception)


class _FakeBackend(BaseGSBackend):
    def __init__(self, batching=False):
        self.client_obj = _FakeClient(batching)

    def client(self):
        return self.client_obj


def _bucket_with(**objects):
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(objects)
    return backend, bucket


class _Buckets(_FakeClient):
    """A client whose buckets are told apart by name."""

    def __init__(self):
        super().__init__()
        self.named = {}

    def bucket(self, name):
        return self.named.setdefault(name, _FakeBucket())


def _two_buckets():
    """A backend whose client has the buckets `one` and `two`, and the two."""
    backend = _FakeBackend()
    backend.client_obj = _Buckets()
    return backend, backend.client_obj.bucket("one"), backend.client_obj.bucket("two")
