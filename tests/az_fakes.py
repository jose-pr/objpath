"""The fakes of the SDK client that more than one test module uses."""

from __future__ import annotations

import datetime
import hashlib
import types
import objstore_fakes
from objpath import BaseAZBackend


class _HttpResponseError(Exception):
    """Stands in for `azure.core.exceptions.HttpResponseError`: the HTTP status
    is the `status_code` attribute, which is what `AZPath` classifies by."""

    status_code = None


class _Missing(_HttpResponseError):  # ResourceNotFoundError
    status_code = 404


class _Exists(_HttpResponseError):  # ResourceExistsError
    status_code = 409


class _ServiceUnavailable(_HttpResponseError):
    status_code = 503


class _ConditionNotMet(_HttpResponseError):  # ResourceModifiedError
    status_code = 412


class _RangeNotSatisfiable(_HttpResponseError):
    status_code = 416


class _Forbidden(_HttpResponseError):
    status_code = 403


class _FakeBlobItem:
    def __init__(self, name, data):
        self.name = name
        self.size = len(data)
        self.last_modified = datetime.datetime(
            2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc
        )


class _FakeDownloader:
    def __init__(self, data, etag='"0"', total=None):
        self._data = data
        self.properties = types.SimpleNamespace(
            etag=etag, content_range=f"bytes 0-{len(data) - 1}/{total}"
        )

    def readall(self):
        return self._data


class _FakeBlobClient:
    def __init__(self, container, name):
        self._container = container
        self.name = name
        self.url = f"https://example.invalid/{name}"

    def _etag(self, etag, match_condition):
        """The blob's current ETag, after the condition: `etag=` with
        `match_condition=` is the If-Match the SDK sends, and a blob that is
        not that one is 412."""
        container = self._container
        if self.name not in container.objects:
            if etag is not None and container.missing_is_412:
                raise _ConditionNotMet(self.name)
            raise _Missing(self.name)
        current = '"%d"' % container.objects.version(self.name)
        if etag is not None:
            assert match_condition is not None
            if etag != current:
                raise _ConditionNotMet(self.name)
        return current

    def get_blob_properties(self, etag=None, match_condition=None):
        if self.name in self._container.properties_errors:
            raise self._container.properties_errors[self.name]
        self._etag(etag, match_condition)
        content = self._container.objects[self.name]
        md5 = self._container.content_md5.get(
            self.name, bytearray(hashlib.md5(content).digest())
        )
        return {
            "size": len(content),
            "last_modified": datetime.datetime(
                2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc
            ),
            "content_settings": {"content_md5": md5},
        }

    def delete_blob(self):
        if self.name in self._container.delete_errors:
            raise OSError(self.name)
        if self.name in self._container.delete_raises:
            raise self._container.delete_raises[self.name]
        if self.name not in self._container.objects:
            raise _Missing(self.name)
        del self._container.objects[self.name]
        self._container.deleted.append(self.name)

    def download_blob(self, offset=None, length=None, etag=None, match_condition=None):
        if self._container.before_read is not None:
            self._container.before_read(self.name)
        current = self._etag(etag, match_condition)
        self._container.reads.append((self.name, offset, etag))
        content = self._container.objects[self.name]
        total = len(content)
        if offset is not None:
            # A range at or past the end is answered 416, an empty blob's
            # included.
            if offset >= len(content):
                raise _RangeNotSatisfiable(self.name)
            content = content[offset : None if length is None else offset + length]
            self._container.ranges.append((self.name, offset, length))
        self._container.downloads.append(self.name)
        return _FakeDownloader(content, current, total)

    def upload_blob(self, data, overwrite=False):
        if self._container.upload_errors:
            raise self._container.upload_errors.pop(0)
        if hasattr(data, "read"):
            self._container.stream_uploads.append(self.name)
            data = data.read()
        # The real SDK sends If-None-Match: * unless overwrite=True.
        if not overwrite and self.name in self._container.objects:
            raise _Exists(self.name)
        self._container.objects[self.name] = bytes(data)
        self._container.uploads.append(self.name)

    def start_copy_from_url(self, url):
        source = url.split("example.invalid/", 1)[1]
        if source in self._container.copy_errors:
            raise self._container.copy_errors[source]
        self._container.objects[self.name] = self._container.objects[source]
        self._container.copies.append((source, self.name))
        return {"copy_status": "success"}


class _FakeContainer:
    def __init__(self, *, bulk=True):
        self.objects = objstore_fakes.VersionedObjects()
        self.before_read = None
        self.missing_is_412 = False
        self.reads = []
        self.ranges = []
        self.stream_uploads = []
        self.copy_errors = {}
        self.copies = []
        self.downloads = []
        self.content_md5 = {}
        self.deleted = []
        self.uploads = []
        self.delete_errors = set()
        self.delete_raises = {}
        self.properties_errors = {}
        self.upload_errors = []
        self.batch_rejected = False
        self.list_calls = []
        self.bulk_delete_calls = []
        if not bulk:
            self.delete_blobs = None

    def get_blob_client(self, name):
        return _FakeBlobClient(self, name)

    def list_blobs(self, name_starts_with="", **_kwargs):
        self.list_calls.append(name_starts_with)
        for name in sorted(self.objects):
            if name.startswith(name_starts_with):
                yield _FakeBlobItem(name, self.objects[name])

    def walk_blobs(self, name_starts_with="", delimiter="/"):
        from azure.storage.blob import BlobPrefix

        # Like the SDK, every prefix of a page comes before its blobs, in
        # whatever order the service listed them.
        prefixes = []
        blobs = []
        for name in sorted(self.objects):
            if not name.startswith(name_starts_with):
                continue
            rest = name[len(name_starts_with) :]
            if delimiter in rest:
                prefix = name_starts_with + rest.split(delimiter, 1)[0] + delimiter
                if prefix not in prefixes:
                    prefixes.append(prefix)
                continue
            blobs.append(_FakeBlobItem(name, self.objects[name]))
        for prefix in prefixes:
            yield BlobPrefix(prefix=prefix)
        yield from blobs

    def delete_blobs(self, *names):
        # Like the real SDK: a rejected batch deletes nothing; otherwise
        # every sub-request runs, then a partial failure raises
        # (PartialBatchErrorException).
        self.bulk_delete_calls.append(names)
        if self.batch_rejected:
            raise _HttpResponseError("batch endpoint rejected")
        failed = []
        for name in names:
            if name in self.delete_errors:
                failed.append(name)
                continue
            if name in self.objects:
                del self.objects[name]
                self.deleted.append(name)
        if failed:
            raise OSError(f"partial batch failure: {failed}")


class _FakeClient:
    """The containers of one account, by name; `container` is the one named
    "container", which most tests fill and inspect."""

    def __init__(self, *, bulk=True):
        self._bulk = bulk
        self.containers = {}
        self.container = self.get_container_client("container")

    def get_container_client(self, name):
        if name not in self.containers:
            self.containers[name] = _FakeContainer(bulk=self._bulk)
        return self.containers[name]


class _FakeBackend(BaseAZBackend):
    account = "account"

    def __init__(self, *, bulk=True):
        self.client_obj = _FakeClient(bulk=bulk)

    def client(self):
        return self.client_obj


def _container_with(bulk=True, **objects):
    backend = _FakeBackend(bulk=bulk)
    container = backend.client_obj.container
    container.objects.update(objects)
    return backend, container
