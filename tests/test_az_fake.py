"""`AZPath` and `AZURIPath` on an in-memory stand-in for the SDK's client: what
a request asks of the service, how each reply and failure becomes an `OSError`,
and what a stream, a copy, a rename and a removal cost in requests. Every test
that takes `az` runs for both classes."""

import errno
import hashlib
import sys
import types

import pytest

from objpath import AZBackend, AZPath, BaseAZBackend
from objpath import _az as az_module
from objpath import _writer
from objpath.exceptions import ObjectChangedError, UnreachableKeysError
from store_support import make_path
from az_fakes import (  # noqa: E402
    _Exists,
    _FakeBackend,
    _FakeBlobClient,
    _FakeBlobItem,
    _FakeContainer,
    _FakeDownloader,
    _Forbidden,
    _HttpResponseError,
    _Missing,
    _ServiceUnavailable,
    _container_with,
)


class _LeaseIdMissing(_HttpResponseError):
    status_code = 412


class _NotImplemented(_HttpResponseError):
    status_code = 501


@pytest.fixture(autouse=True)
def _fresh_default_backends():
    """The default backends are cached per account for the process; a test
    starts with none and leaves none."""
    from objpath import _az_backend

    kept = dict(_az_backend._DEFAULT_BACKENDS)
    _az_backend._DEFAULT_BACKENDS.clear()
    yield
    _az_backend._DEFAULT_BACKENDS.clear()
    _az_backend._DEFAULT_BACKENDS.update(kept)


@pytest.fixture
def walk_sdk(monkeypatch):
    """`azure.storage.blob.BlobPrefix`, which a `walk_blobs()` listing is made
    of: the SDK's own when it is installed, else a stand-in that is named from
    its `prefix=` argument as the SDK's is."""
    import importlib

    try:
        importlib.import_module("azure.storage.blob")
    except ImportError:
        pass
    else:
        return

    class BlobPrefix:
        def __init__(self, *args, prefix=None, **kwargs):
            self.name = prefix

    blob = types.ModuleType("azure.storage.blob")
    blob.BlobPrefix = BlobPrefix
    for name in ("azure", "azure.storage"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob)


@pytest.fixture
def az(flavor):
    """Builds the path a URL names, of the class `flavor` selects, on `backend`
    (a fresh fake one when none is given)."""

    def build(url, backend=None):
        return make_path(flavor, url, backend=backend or _FakeBackend())

    return build


def test_stat_reports_the_last_modified_time_as_an_epoch(az):
    # `last_modified` is a timezone-aware UTC datetime.
    backend, _container = _container_with(**{"dir/a.txt": b"abc"})
    assert az("az://account/container/dir/a.txt", backend).stat().st_mtime == 1767268800


def test_listing_reports_the_last_modified_time_as_an_epoch(az):
    # The listing tells blobs from prefixes with the SDK's own `BlobPrefix`.
    pytest.importorskip("azure.storage.blob")
    backend, _container = _container_with(**{"dir/a.txt": b"abc"})
    entries = dict(az("az://account/container/dir", backend)._scandir())
    assert entries["a.txt"].st_mtime == 1767268800
    assert entries["a.txt"].st_size == 3


def test_rm_recursive_deletes_prefix_tree_with_bulk_delete(az):
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects.update(
        {
            "dir/": b"",
            "dir/a.txt": b"a",
            "dir/sub/b.txt": b"b",
            "other.txt": b"keep",
        }
    )
    az("az://account/container/dir", backend).rm(recursive=True)
    assert container.objects == {"other.txt": b"keep"}
    assert container.list_calls == ["dir/"]
    assert container.bulk_delete_calls == [("dir/", "dir/a.txt", "dir/sub/b.txt")]


def test_rm_recursive_deletes_exact_object_before_prefix(az):
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects.update(
        {
            "file.txt": b"x",
            "file.txt/nested.txt": b"keep",
        }
    )
    az("az://account/container/file.txt", backend).rm(recursive=True)
    assert container.objects == {"file.txt/nested.txt": b"keep"}
    assert container.list_calls == []


def test_rm_recursive_missing_ok(az):
    az("az://account/container/missing", _FakeBackend()).rm(
        recursive=True, missing_ok=True
    )


def test_rm_recursive_missing_without_missing_ok_raises(az):
    with pytest.raises(FileNotFoundError):
        az("az://account/container/missing", _FakeBackend()).rm(recursive=True)


def test_rm_recursive_ignore_error_swallows_missing(az):
    calls = []
    az("az://account/container/missing", _FakeBackend()).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(FileNotFoundError, "missing")]


def test_rm_recursive_root_guard(az):
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects["a.txt"] = b"x"
    with pytest.raises(PermissionError):
        az("az://account/container/", backend).rm(recursive=True)
    assert container.objects == {"a.txt": b"x"}


def test_rm_recursive_delete_error_reroutes_to_ignore_error(az):
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects["dir/a.txt"] = b"x"
    container.delete_errors.add("dir/a.txt")
    calls = []
    az("az://account/container/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(OSError, "dir/a.txt")]


# --- a trailing "/" names the directory; interior empty segments stay -------


@pytest.mark.parametrize(
    "uri, container, key",
    [
        ("az://account/container/dir/", "container", "dir"),
        ("az://account/container/dir", "container", "dir"),
        ("az://account/container/", "container", ""),
        ("az://account/container", "container", ""),
        # An interior empty segment is part of the blob name.
        ("az://account/container/a//b", "container", "a//b"),
        ("az://account/container/dir//", "container", "dir/"),
    ],
)
def test_container_and_key_drop_one_trailing_slash(az, uri, container, key):
    p = az(uri)
    assert p.container == container
    assert p.key == key


def test_trailing_slash_marker_dir_is_a_directory_and_rm_deletes_tree(az):
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects.update(
        {"dir/": b"", "dir/a.txt": b"a", "dir/sub/b.txt": b"b", "other.txt": b"k"}
    )
    p = az("az://account/container/dir/", backend)
    assert p.is_dir()
    assert not p.is_file()
    p.rm(recursive=True)
    assert container.objects == {"other.txt": b"k"}


# --- only a not-found reply means "missing" ---------------------------------


def test_transient_properties_error_is_not_file_not_found(az):
    backend, container = _container_with(**{"report.csv": b"PRECIOUS"})
    container.properties_errors["report.csv"] = _ServiceUnavailable("503")
    p = az("az://account/container/report.csv", backend)
    with pytest.raises(OSError) as info:
        p.stat()
    assert not isinstance(info.value, FileNotFoundError)
    assert info.value.__cause__ is None
    assert container.objects == {"report.csv": b"PRECIOUS"}


def test_rm_recursive_transient_properties_error_keeps_prefix_tree(az):
    backend, container = _container_with(**{"x": b"object", "x/child": b"c"})
    container.properties_errors["x"] = _ServiceUnavailable("503")
    with pytest.raises(OSError):
        az("az://account/container/x", backend).rm(recursive=True)
    assert container.objects == {"x": b"object", "x/child": b"c"}


def test_unlink_under_lease_is_not_hidden_or_file_exists(az):
    backend, container = _container_with(**{"leased.txt": b"x"})
    container.delete_raises["leased.txt"] = _LeaseIdMissing("412 lease")
    p = az("az://account/container/leased.txt", backend)
    for kwargs in ({"missing_ok": True}, {}):
        with pytest.raises(OSError) as info:
            p.unlink(**kwargs)
        assert type(info.value) is OSError
    assert "leased.txt" in container.objects


def test_missing_sdk_is_import_error_not_file_not_found(flavor, monkeypatch):
    # An entry of None in `sys.modules` makes the import of that name fail.
    monkeypatch.setitem(sys.modules, "azure.storage.blob", None)
    path = make_path(flavor, "az://account/container/a.txt")
    with pytest.raises(ImportError, match=r"objpath\[az\]"):
        path.read_bytes()
    with pytest.raises(ImportError, match=r"objpath\[az\]"):
        path.stat()


# --- the options of a client reach it unchanged ------------------------------


def test_connection_string_keeps_the_other_kwargs(walk_sdk, monkeypatch):
    import importlib

    built = []

    class BlobServiceClient:
        @classmethod
        def from_connection_string(cls, conn_str, credential=None, **kwargs):
            built.append((conn_str, credential, kwargs))
            return object.__new__(cls)

    monkeypatch.setattr(
        importlib.import_module("azure.storage.blob"),
        "BlobServiceClient",
        BlobServiceClient,
    )
    AZBackend(
        connection_string="cs", credential="token", max_single_put_size=4
    ).client()
    assert built == [("cs", "token", {"max_single_put_size": 4})]


# --- missing or wrong-type targets raise like pathlib ------------------------


def test_iterdir_missing_and_file_raise(az, walk_sdk):
    backend, _container = _container_with(**{"file.txt": b"x", "d/": b""})
    with pytest.raises(FileNotFoundError):
        list(az("az://account/container/typo", backend).iterdir())
    with pytest.raises(NotADirectoryError):
        list(az("az://account/container/file.txt", backend).iterdir())
    assert list(az("az://account/container/d", backend).iterdir()) == []


def test_unlink_directory_and_rmdir_wrong_targets(az):
    import errno

    backend, container = _container_with(**{"file.txt": b"x", "d/": b"", "d/f": b"y"})
    with pytest.raises(IsADirectoryError):
        az("az://account/container/d", backend).unlink()
    with pytest.raises(FileNotFoundError):
        az("az://account/container/nope", backend).rmdir()
    with pytest.raises(NotADirectoryError):
        az("az://account/container/file.txt", backend).rmdir()
    with pytest.raises(OSError) as info:
        az("az://account/container/d", backend).rmdir()
    assert info.value.errno == errno.ENOTEMPTY
    assert set(container.objects) == {"file.txt", "d/", "d/f"}


# --- write streams ----------------------------------------------------------


def test_failed_close_upload_is_not_retried_at_gc(az):
    import gc

    backend, container = _container_with()
    container.upload_errors.append(_ServiceUnavailable("503"))
    p = az("az://account/container/state.json", backend)
    f = p.open("wb")
    f.write(b'{"version": 1}')
    with pytest.raises(OSError):
        f.close()
    assert f.closed
    p.write_bytes(b'{"version": 2}')
    del f
    gc.collect()
    assert container.objects["state.json"] == b'{"version": 2}'


def test_exclusive_create_is_atomic(az):
    backend, container = _container_with()
    first = az("az://account/container/job.lock", backend).open("xb")
    second = az("az://account/container/job.lock", backend).open("xb")
    first.write(b"worker1")
    second.write(b"worker2")
    first.close()
    with pytest.raises(FileExistsError):
        second.close()
    assert container.objects["job.lock"] == b"worker1"


def test_mkdir_race_is_file_exists(az):
    backend, container = _container_with()
    container.upload_errors.append(_Exists("409 BlobAlreadyExists"))
    with pytest.raises(FileExistsError):
        az("az://account/container/d", backend).mkdir()


def test_rplus_writes_land_and_read_stream_is_read_only(az):
    import io

    backend, container = _container_with(**{"a.txt": b"hello"})
    p = az("az://account/container/a.txt", backend)
    with p.open("rb") as f:
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"x")
    with p.open("r+b") as f:
        assert f.read() == b"hello"
    assert container.uploads == []
    with p.open("r+b") as f:
        f.write(b"J")
    assert container.objects["a.txt"] == b"Jello"


class _Abort(Exception):
    pass


@pytest.mark.parametrize("spooled", [False, True], ids=["memory", "temp-file"])
@pytest.mark.parametrize("mode, data", [("wb", b"part"), ("w", "part")])
def test_a_write_block_that_raised_uploads_nothing(
    az, monkeypatch, mode, data, spooled
):
    if spooled:
        monkeypatch.setattr(_writer.SpoolUploadStream, "spool_size", 2)
    backend, container = _container_with(**{"old": b"OLD"})
    for key in ("old", "new"):
        with pytest.raises(_Abort):
            with az(f"az://account/container/{key}", backend).open(mode) as f:
                f.write(data)
                raise _Abort
    assert container.objects == {"old": b"OLD"}
    assert container.uploads == []


@pytest.mark.parametrize("mode, data", [("r+b", b"J"), ("r+", "J")])
def test_a_read_modify_write_block_that_raised_uploads_nothing(az, mode, data):
    backend, container = _container_with(**{"a.txt": b"hello"})
    with pytest.raises(_Abort):
        with az("az://account/container/a.txt", backend).open(mode) as f:
            f.write(data)
            raise _Abort
    assert container.objects == {"a.txt": b"hello"}
    assert container.uploads == []


def test_an_exclusive_create_block_that_raised_uploads_nothing(az):
    backend, container = _container_with()
    with pytest.raises(_Abort):
        with az("az://account/container/job.lock", backend).open("xb") as f:
            f.write(b"worker1")
            raise _Abort
    assert container.objects == {} and container.uploads == []


@pytest.mark.parametrize("mode, data", [("wb", b"whole"), ("w", "whole")])
def test_a_write_block_that_ended_normally_or_was_closed_uploads_once(az, mode, data):
    backend, container = _container_with()
    with az("az://account/container/a", backend).open(mode) as f:
        f.write(data)
    f = az("az://account/container/b", backend).open(mode)
    f.write(data)
    f.close()
    f.close()
    assert container.objects == {"a": b"whole", "b": b"whole"}
    assert len(container.uploads) == 2


@pytest.mark.parametrize("mode, data", [("wb", b"whole"), ("w", "whole")])
def test_an_upload_that_fails_after_a_normal_block_raises_once_and_is_not_retried(
    az, mode, data
):
    import gc

    backend, container = _container_with()
    container.upload_errors.append(_ServiceUnavailable("503"))
    with pytest.raises(OSError):
        with az("az://account/container/state.json", backend).open(mode) as f:
            f.write(data)
    gc.collect()
    assert container.objects == {} and container.uploads == []


# --- rename/move of a prefix directory ---------------------------------------


def test_rename_prefix_directory_falls_back_in_move(az, walk_sdk):
    backend, container = _container_with(**{"dir/a": b"a", "dir/sub/b": b"b"})
    src = az("az://account/container/dir", backend)
    with pytest.raises(NotImplementedError):
        src.rename("dir2")
    with pytest.raises(FileNotFoundError):
        az("az://account/container/nope", backend).rename("nope2")
    src.move(az("az://account/container/dir3", backend))
    assert container.objects == {
        "dir3/": b"",
        "dir3/a": b"a",
        "dir3/sub/": b"",
        "dir3/sub/b": b"b",
    }


# --- bulk delete partial failure -----------------------------------------------


@pytest.mark.parametrize(
    "bulk, batch_rejected",
    [(True, False), (True, True), (False, False)],
    ids=["partial-batch", "rejected-batch", "no-bulk"],
)
def test_rm_recursive_one_failing_blob_does_not_leave_the_rest(
    az, bulk, batch_rejected
):
    backend, container = _container_with(
        bulk=bulk, **{"dir/a.txt": b"a", "dir/b.txt": b"b", "dir/c.txt": b"c"}
    )
    container.batch_rejected = batch_rejected
    container.delete_errors.add("dir/a.txt")
    calls = []
    az("az://account/container/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append(str(err)) or True,
    )
    assert container.objects == {"dir/a.txt": b"a"}
    assert calls == ["dir/a.txt"]


# --- a blob and a prefix of one name: the listing shows the blob -------------


def test_key_that_is_object_and_prefix_lists_as_the_object(az, walk_sdk):
    backend, _container = _container_with(
        **{"src/logs": b"FILE-CONTENT", "src/logs/2026.txt": b"child", "src/d/x": b"x"}
    )
    listing = dict(az("az://account/container/src", backend)._scandir())
    assert sorted(listing) == ["d", "logs"]
    assert not listing["logs"].is_dir()
    assert listing["d"].is_dir()
    assert not az("az://account/container/src/logs", backend).stat().is_dir()


# --- the root of a container exists only when the service says so -----------


def test_missing_container_root_does_not_exist(az):
    backend, _container = _container_with()

    def missing(name_starts_with="", **_kwargs):
        raise _Missing("ContainerNotFound")

    backend.client_obj.get_container_client("typo-container").list_blobs = missing
    root = az("az://account/typo-container", backend)
    with pytest.raises(FileNotFoundError):
        root.stat()
    assert not root.exists()


def test_existing_container_root_is_a_directory_even_when_empty(az):
    backend, container = _container_with()
    assert az("az://account/container/", backend).stat().is_dir()
    assert container.list_calls == [""]


# --- a rejected batch ---------------------------------------------------------


def test_rejected_batch_falls_back_per_blob_with_each_blobs_error(az):
    backend, container = _container_with(**{f"dir/f{i}.txt": b"x" for i in range(5)})
    container.batch_rejected = True
    container.delete_raises["dir/f0.txt"] = _LeaseIdMissing("lease held")
    seen = []
    az("az://account/container/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: seen.append(type(err).__name__) or True,
    )
    assert sorted(container.objects) == ["dir/f0.txt"]
    assert seen == ["OSError"]


# --- a listed name is one component inside the directory that listed it ------


def test_listing_skips_names_that_are_not_one_component(az, walk_sdk):
    backend, _container = _container_with(
        **{
            "dir/ok.txt": b"x",
            "dir/..": b"x",
            "dir/.": b"x",
            "dir/../up/f": b"x",
            "dir/./down/f": b"x",
            "dir/sub/f": b"x",
        }
    )
    listing = dict(az("az://account/container/dir", backend)._scandir())
    assert sorted(listing) == ["ok.txt", "sub"]
    assert listing["sub"].is_dir()


def test_listing_skips_names_a_server_reports_with_a_separator(az, walk_sdk):
    from azure.storage.blob import BlobPrefix

    backend, container = _container_with(**{"dir/ok.txt": b"x", "dir/a/b": b"x"})

    def raw(name_starts_with="", delimiter="/"):
        yield _FakeBlobItem("dir/ok.txt", b"x")
        yield _FakeBlobItem("dir/a/b", b"x")
        for prefix in ("dir/sub/", "dir/p/q/", "dir//"):
            yield BlobPrefix(prefix=prefix)

    container.walk_blobs = raw
    listing = dict(az("az://account/container/dir", backend)._scandir())
    assert sorted(listing) == ["ok.txt", "sub"]


# --- a prefix is moved, copied and removed as the same set of blobs ----------

_ODD_PREFIXES = {
    "an-empty-segment": {"d/x.txt": b"x", "d//y.txt": b"y", "d/sub//z.txt": b"z"},
    "a-blob-that-is-also-a-prefix": {
        "d/x.txt": b"x",
        "d/logs": b"log",
        "d/logs/2026.txt": b"c",
        "d/logs/deep/z.txt": b"z",
    },
    "a-slash-name-that-holds-data": {"d/x.txt": b"x", "d/blob/": b"data"},
    "a-dot-segment": {"d/x.txt": b"x", "d/sub/../z.txt": b"z"},
}


@pytest.fixture(params=list(_ODD_PREFIXES))
def odd_prefix(request):
    backend, container = _container_with(**_ODD_PREFIXES[request.param])
    container.objects["unrelated.txt"] = b"keep"
    return backend, container, dict(container.objects)


def test_rm_recursive_of_a_prefix_with_hidden_blobs_removes_none(az, odd_prefix):
    backend, container, before = odd_prefix
    with pytest.raises(UnreachableKeysError, match="nothing was changed"):
        az("az://account/container/d", backend).rm(recursive=True)
    assert container.objects == before
    assert container.deleted == []


def test_copy_recursive_of_a_prefix_with_hidden_blobs_is_refused(az, odd_prefix):
    from pathlib_next.mempath import MemPath

    backend, container, before = odd_prefix
    target = MemPath("/copied")
    with pytest.raises(UnreachableKeysError, match="nothing was changed"):
        az("az://account/container/d", backend).copy(target, recursive=True)
    assert not target.exists()
    assert container.objects == before


def test_move_of_a_prefix_with_hidden_blobs_keeps_every_blob(az, odd_prefix):
    backend, container, before = odd_prefix
    with pytest.raises(UnreachableKeysError, match="nothing was changed"):
        az("az://account/container/d", backend).move(
            az("az://account/container/moved", backend)
        )
    assert container.objects == before


def test_the_refusal_names_the_blobs_a_listing_does_not_show(az):
    backend, container = _container_with(
        **{"d/x.txt": b"x", "d//y.txt": b"y", "d/logs": b"l", "d/logs/2026.txt": b"c"}
    )
    with pytest.raises(UnreachableKeysError) as raised:
        az("az://account/container/d", backend).rm(recursive=True)
    assert raised.value.errno == errno.EINVAL
    assert "'d//y.txt'" in str(raised.value)
    assert "'d/logs/2026.txt'" in str(raised.value)
    assert "'d/x.txt'" not in str(raised.value)


def test_rm_recursive_with_ignore_error_removes_only_the_blobs_a_listing_shows(az):
    backend, container = _container_with(
        **{
            "d/": b"",
            "d/x.txt": b"x",
            "d//y.txt": b"y",
            "d/logs": b"l",
            "d/logs/z": b"z",
        }
    )
    seen = []
    az("az://account/container/d", backend).rm(
        recursive=True, ignore_error=lambda err, path: seen.append(err) or True
    )
    assert len(seen) == 1 and "'d//y.txt'" in str(seen[0])
    assert sorted(container.objects) == ["d//y.txt", "d/logs/z"]


def test_move_of_a_prefix_a_listing_shows_moves_every_blob(az, walk_sdk):
    backend, container = _container_with(
        **{"d/": b"", "d/x.txt": b"x", "d/sub/a.txt": b"a", "d/empty/": b""}
    )
    az("az://account/container/d", backend).move(
        az("az://account/container/moved", backend)
    )
    assert sorted(container.objects) == [
        "moved/",
        "moved/empty/",
        "moved/sub/",
        "moved/sub/a.txt",
        "moved/x.txt",
    ]


def test_rm_recursive_of_a_prefix_a_listing_shows_is_one_batched_delete(az):
    backend, container = _container_with(
        **{"d/": b"", "d/x.txt": b"x", "d/sub/a.txt": b"a", "d/empty/": b"", "k": b""}
    )
    az("az://account/container/d", backend).rm(recursive=True)
    assert container.bulk_delete_calls == [("d/", "d/empty/", "d/sub/a.txt", "d/x.txt")]
    assert container.objects == {"k": b""}


# --- a key that is both a blob and a prefix lists as the blob ----------------


def test_blob_wins_over_a_prefix_of_the_same_name_whatever_the_listing_order(
    az, walk_sdk
):
    backend, container = _container_with(
        **{"src/logs": b"FILE-CONTENT", "src/logs/2026.txt": b"child", "src/d/x": b"x"}
    )
    names = [
        item.name
        for item in container.walk_blobs(name_starts_with="src/", delimiter="/")
    ]
    # The SDK puts every prefix of a page before its blobs.
    assert names == ["src/d/", "src/logs/", "src/logs"]
    listing = dict(az("az://account/container/src", backend)._scandir())
    assert sorted(listing) == ["d", "logs"]
    assert not listing["logs"].is_dir()
    assert listing["logs"].st_size == len(b"FILE-CONTENT")


# --- a write or rename never turns a prefix directory into a blob -------------


def test_write_onto_a_prefix_directory_is_refused(az):
    backend, container = _container_with(**{"d/": b"", "d/x.txt": b"x"})
    with pytest.raises(IsADirectoryError):
        az("az://account/container/d", backend).write_bytes(b"clobber")
    assert container.objects == {"d/": b"", "d/x.txt": b"x"}
    assert container.uploads == []


def test_rename_onto_a_prefix_directory_is_refused(az):
    backend, container = _container_with(**{"b.txt": b"b", "dst/keep.txt": b"k"})
    with pytest.raises(IsADirectoryError):
        az("az://account/container/b.txt", backend).rename("dst")
    assert container.objects == {"b.txt": b"b", "dst/keep.txt": b"k"}


def test_write_and_rename_onto_a_blob_still_replace_it(az):
    backend, container = _container_with(**{"a.txt": b"a", "b.txt": b"b"})
    az("az://account/container/a.txt", backend).write_bytes(b"new")
    assert container.objects["a.txt"] == b"new"
    az("az://account/container/b.txt", backend).rename("a.txt")
    assert container.objects == {"a.txt": b"b"}


def test_write_where_the_prefix_cannot_be_listed_still_writes(az):
    backend, container = _container_with()

    def denied(name_starts_with="", **_kwargs):
        raise _Forbidden("AuthorizationFailure")

    container.list_blobs = denied
    az("az://account/container/w.txt", backend).write_bytes(b"written")
    assert container.objects == {"w.txt": b"written"}


# --- mkdir treats only "not found" as absent -----------------------------------


def test_mkdir_does_not_create_a_marker_when_the_probe_fails(az):
    backend, container = _container_with(**{"existing.txt": b"keep"})
    container.properties_errors["existing.txt"] = _ServiceUnavailable("503")
    with pytest.raises(OSError) as raised:
        az("az://account/container/existing.txt", backend).mkdir()
    assert not isinstance(raised.value, FileExistsError)
    assert container.objects == {"existing.txt": b"keep"}
    assert container.uploads == []


def test_mkdir_on_a_blob_is_file_exists_and_on_a_missing_path_writes_the_marker(az):
    backend, container = _container_with(**{"existing.txt": b"keep"})
    with pytest.raises(FileExistsError):
        az("az://account/container/existing.txt", backend).mkdir()
    az("az://account/container/newdir", backend).mkdir()
    assert sorted(container.objects) == ["existing.txt", "newdir/"]


# --- removing a container is not supported --------------------------------------


def test_rmdir_of_the_container_root_is_refused(az):
    backend, container = _container_with(**{"a.txt": b"x"})
    root = az("az://account/container/", backend)
    with pytest.raises(PermissionError):
        root.rmdir()
    with pytest.raises(PermissionError):
        root.rm()
    assert container.objects == {"a.txt": b"x"}
    assert container.deleted == []


# --- a failed request is an OSError of the right type, with nothing chained --


class _GatewayTimeout(_HttpResponseError):
    status_code = 504


def _properties_failure(az, error):
    backend, container = _container_with(**{"a.txt": b"x"})
    container.properties_errors["a.txt"] = error
    return az("az://account/container/a.txt", backend)


def _azure_core():
    return pytest.importorskip("azure.core.exceptions")


def test_a_connection_failure_is_a_connection_error_naming_the_path(az):
    azexc = _azure_core()
    error = azexc.ServiceRequestError("refused: /c/a.txt?sv=1&sig=SECRET")
    path = _properties_failure(az, error)
    with pytest.raises(ConnectionError) as info:
        path.stat()
    assert not isinstance(info.value, (TimeoutError, ConnectionResetError))
    assert info.value.filename == "az://account/container/a.txt"
    assert info.value.__cause__ is None
    assert "SECRET" not in str(info.value)
    assert path.exists() is False
    backend, container = _container_with(**{"a.txt": b"x"})

    def refused(*args, **kwargs):
        raise error

    container.walk_blobs = refused
    errors = []
    root = az("az://account/container", backend)
    assert list(root.walk(on_error=errors.append)) == []
    assert [type(e) for e in errors] == [ConnectionError]


def test_a_request_that_times_out_is_a_timeout_error(az):
    azexc = _azure_core()
    requests_exc = pytest.importorskip("requests.exceptions")
    errors = [
        azexc.ServiceRequestError("slow", error=requests_exc.ConnectTimeout("c")),
        azexc.ServiceResponseError("slow", error=requests_exc.ReadTimeout("r")),
    ]
    for name in ("ServiceRequestTimeoutError", "ServiceResponseTimeoutError"):
        if hasattr(azexc, name):
            errors.append(getattr(azexc, name)("slow"))
    for error in errors:
        path = _properties_failure(az, error)
        with pytest.raises(TimeoutError) as info:
            path.stat()
        assert info.value.filename == "az://account/container/a.txt"
        assert info.value.__cause__ is None


def test_a_gateway_timeout_reply_is_a_timeout_error(az):
    path = _properties_failure(az, _GatewayTimeout("504"))
    with pytest.raises(TimeoutError):
        path.stat()


def test_a_response_that_does_not_arrive_whole_is_a_connection_reset(az):
    azexc = _azure_core()
    errors = [azexc.ServiceResponseError("Connection aborted")]
    if hasattr(azexc, "IncompleteReadError"):
        errors.append(azexc.IncompleteReadError("short body"))
    for error in errors:
        path = _properties_failure(az, error)
        with pytest.raises(ConnectionResetError) as info:
            path.stat()
        assert info.value.filename == "az://account/container/a.txt"
        assert info.value.__cause__ is None


def test_a_body_cut_short_is_a_connection_reset_naming_the_path(az, monkeypatch):
    azexc = _azure_core()
    backend, container = _container_with(**{"a.txt": b"x"})

    def cut(self):
        raise azexc.ServiceResponseError("Connection broken")

    monkeypatch.setattr(_FakeDownloader, "readall", cut)
    with pytest.raises(ConnectionResetError) as info:
        az("az://account/container/a.txt", backend).read_bytes()
    assert info.value.filename == "az://account/container/a.txt"
    assert info.value.__cause__ is None


def test_other_exceptions_still_propagate_as_themselves(az):
    path = _properties_failure(az, KeyError("a bug"))
    with pytest.raises(KeyError):
        path.stat()


# --- paths that name no blob ---------------------------------------------------


def test_a_container_root_is_a_directory_to_read_or_write(az):
    backend, container = _container_with(**{"a.txt": b"x"})
    root = az("az://account/container/", backend)
    with pytest.raises(IsADirectoryError):
        root.read_bytes()
    with pytest.raises(IsADirectoryError):
        root.write_bytes(b"clobber")
    with pytest.raises(IsADirectoryError):
        root.open("r+b")
    assert container.list_calls == [] and container.uploads == []
    assert container.objects == {"a.txt": b"x"}


def test_removing_a_missing_container_with_missing_ok_is_not_an_error(az):
    backend, _container = _container_with()

    def missing(name_starts_with="", **_kwargs):
        raise _Missing("ContainerNotFound")

    backend.client_obj.get_container_client("nocontainer").list_blobs = missing
    path = az("az://account/nocontainer/x", backend)
    assert path.rm(recursive=True, missing_ok=True) is None
    with pytest.raises(FileNotFoundError):
        path.rm(recursive=True)
    offered = []
    path.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append(type(error)) or True,
    )
    assert offered == [FileNotFoundError]


def test_renaming_a_missing_path_onto_its_own_name_is_file_not_found(az):
    backend, container = _container_with(**{"there.txt": b"x", "d/f": b"x"})
    with pytest.raises(FileNotFoundError):
        az("az://account/container/nope", backend).rename("nope")
    there = az("az://account/container/there.txt", backend)
    assert there.rename("there.txt") == there
    assert az("az://account/container/d", backend).rename("d").key == "d"
    assert container.objects == {"there.txt": b"x", "d/f": b"x"}


def test_a_rename_to_a_name_is_a_rename_to_a_sibling(az):
    backend, container = _container_with(**{"dir/a.txt": b"content", "top.txt": b"t"})
    renamed = az("az://account/container/dir/a.txt", backend).rename("b.txt")
    assert renamed.key == "dir/b.txt"
    assert container.objects == {"dir/b.txt": b"content", "top.txt": b"t"}
    assert container.copies == [("dir/a.txt", "dir/b.txt")]


def test_a_core_path_renames_only_inside_one_container_of_one_backend():
    from pathlib_next.mempath import MemPath

    backend, container = _container_with(**{"a.txt": b"content"})
    other_backend, _other = _container_with()
    source = AZPath("a.txt", container="container", backend=backend)
    for target in (
        AZPath("b.txt", container="elsewhere", backend=backend),
        AZPath("b.txt", container="container", backend=other_backend),
        MemPath("/b.txt"),
    ):
        with pytest.raises(NotImplementedError):
            source.rename(target)
    assert container.objects == {"a.txt": b"content"}
    assert container.copies == []
    # move() copies and removes instead.
    source.move(AZPath("b.txt", container="elsewhere", backend=backend))
    assert backend.client_obj.containers["elsewhere"].objects == {"b.txt": b"content"}
    assert container.objects == {} and container.copies == []
    # Inside one container the service copies.
    inside = AZPath("c.txt", container="elsewhere", backend=backend)
    moved = AZPath("b.txt", container="elsewhere", backend=backend).rename(inside)
    assert moved == inside
    assert backend.client_obj.containers["elsewhere"].objects == {"c.txt": b"content"}


def test_renaming_a_prefix_directory_asks_for_the_properties_once(az, monkeypatch):
    asked = []
    original = _FakeBlobClient.get_blob_properties

    def counted(self):
        asked.append(self.name)
        return original(self)

    monkeypatch.setattr(_FakeBlobClient, "get_blob_properties", counted)
    backend, container = _container_with(**{"dir/f": b"x"})
    with pytest.raises(NotImplementedError):
        az("az://account/container/dir", backend).rename("other")
    assert asked == ["dir"]
    assert container.list_calls == ["dir/"]


def _record_listings(monkeypatch):
    """Every `list_blobs()` call of the fake container, with its page size."""
    pages = []
    original = _FakeContainer.list_blobs

    def recorded(self, name_starts_with="", **kwargs):
        pages.append((name_starts_with, kwargs.get("results_per_page")))
        return original(self, name_starts_with)

    monkeypatch.setattr(_FakeContainer, "list_blobs", recorded)
    return pages


def test_the_existence_probes_ask_for_one_item_not_a_page(az, monkeypatch):
    pages = _record_listings(monkeypatch)
    backend, container = _container_with(**{"dir/f": b"x"})
    root = az("az://account/container/", backend)
    assert (root / "dir").is_dir()
    assert not (root / "missing").exists()
    (root / "new").write_bytes(b"x")
    (root / "made").mkdir()
    with pytest.raises(IsADirectoryError):
        (root / "dir").write_bytes(b"x")
    assert pages
    assert {page for _prefix, page in pages} == {1}
    assert {prefix for prefix, _page in pages} >= {"dir/", "missing/", "new/", "made/"}


def test_the_emptiness_check_of_rmdir_asks_for_two_items(az, monkeypatch):
    pages = _record_listings(monkeypatch)
    backend, container = _container_with(**{"empty/": b""})
    az("az://account/container/empty", backend).rmdir()
    assert container.objects == {}
    assert pages == [("empty/", 1), ("empty/", 2)]


def test_a_copy_that_stays_pending_is_aborted_and_rename_times_out(az, monkeypatch):
    aborted = []
    monkeypatch.setattr(_FakeBackend, "copy_timeout", 0.2)
    monkeypatch.setattr(az_module, "_COPY_POLL_INTERVAL", 0.01)

    def start(self, url):
        return {"copy_status": "pending", "copy_id": "copy-1"}

    def pending(self):
        return {"size": 1, "last_modified": None, "copy_status": "pending"}

    monkeypatch.setattr(_FakeBlobClient, "start_copy_from_url", start)
    monkeypatch.setattr(
        _FakeBlobClient,
        "abort_copy",
        lambda self, copy_id: aborted.append(copy_id),
        raising=False,
    )
    backend, container = _container_with(**{"a.txt": b"x"})
    monkeypatch.setattr(_FakeBlobClient, "get_blob_properties", pending)
    with pytest.raises(TimeoutError) as info:
        az("az://account/container/a.txt", backend).rename("b.txt")
    assert info.value.filename == "az://account/container/a.txt"
    assert aborted == ["copy-1"]
    assert container.objects == {"a.txt": b"x"}


# --- checksum(): the Content-MD5 the service keeps --------------------------


def test_checksum_is_the_content_md5_the_service_keeps(az):
    backend, container = _container_with(**{"a.bin": b"content"})
    path = az("az://account/container/a.bin", backend)
    assert path.checksum() == hashlib.md5(b"content").hexdigest()
    assert path.supported_checksums() == frozenset({"md5"})
    assert container.downloads == []


def test_checksum_of_a_blob_without_a_content_md5_is_refused(az):
    backend, container = _container_with(**{"big.bin": b"content"})
    container.content_md5["big.bin"] = None
    with pytest.raises(NotImplementedError):
        az("az://account/container/big.bin", backend).checksum("md5")


def test_checksum_refuses_an_algorithm_the_service_does_not_keep(az):
    backend, _container = _container_with(**{"a.bin": b"content"})
    with pytest.raises(NotImplementedError):
        az("az://account/container/a.bin", backend).checksum("sha256")


def test_checksum_of_a_missing_path_a_directory_and_the_container(az):
    backend, _container = _container_with(**{"d/a.txt": b"x"})
    with pytest.raises(FileNotFoundError):
        az("az://account/container/nothing", backend).checksum()
    with pytest.raises(IsADirectoryError):
        az("az://account/container/d", backend).checksum()
    with pytest.raises(IsADirectoryError):
        az("az://account/container", backend).checksum()


# --- copy(): one copy call in the service -----------------------------------


def test_copy_of_a_blob_is_one_service_copy_and_reads_nothing(az):
    backend, container = _container_with(**{"a.txt": b"content"})
    az("az://account/container/a.txt", backend).copy(
        az("az://account/container/b.txt", backend)
    )
    assert container.objects["b.txt"] == b"content"
    assert container.copies == [("a.txt", "b.txt")]
    assert container.downloads == [] and container.uploads == []


def test_copy_reports_progress_once_with_the_total(az):
    backend, _container = _container_with(**{"a.txt": b"content", "empty": b""})
    calls = []
    for name, size in (("a.txt", 7), ("empty", 0)):
        az(f"az://account/container/{name}", backend).copy(
            az(f"az://account/container/copy-{name}", backend),
            progress=lambda path, copied, total: calls.append(
                (path.key, copied, total)
            ),
        )
        assert calls == [(name, size, size)]
        del calls[:]


def test_copy_onto_an_existing_blob_needs_overwrite_and_deletes_nothing_first(az):
    backend, container = _container_with(**{"a.txt": b"new", "b.txt": b"old"})
    source = az("az://account/container/a.txt", backend)
    target = az("az://account/container/b.txt", backend)
    with pytest.raises(FileExistsError):
        source.copy(target)
    assert container.copies == [] and container.objects["b.txt"] == b"old"
    source.copy(target, overwrite=True)
    assert container.objects["b.txt"] == b"new"
    assert container.copies == [("a.txt", "b.txt")] and container.deleted == []


def test_copy_onto_itself_or_a_prefix_directory_is_refused_before_the_service_is_asked(
    az,
):
    backend, container = _container_with(**{"a.txt": b"keep", "d/inner": b"inner"})
    source = az("az://account/container/a.txt", backend)
    for overwrite in (False, True):
        with pytest.raises(OSError, match="same file") as info:
            source.copy(
                az("az://account/container/a.txt", backend), overwrite=overwrite
            )
        assert info.value.errno == errno.EINVAL
        with pytest.raises(IsADirectoryError):
            source.copy(az("az://account/container/d", backend), overwrite=overwrite)
    assert container.copies == []
    assert set(container.objects) == {"a.txt", "d/inner"}


def test_a_refused_copy_leaves_an_existing_target_as_it_was(az):
    backend, container = _container_with(**{"a.txt": b"new", "b.txt": b"old"})
    container.copy_errors["a.txt"] = _Forbidden("403")
    with pytest.raises(PermissionError) as info:
        az("az://account/container/a.txt", backend).copy(
            az("az://account/container/b.txt", backend), overwrite=True
        )
    assert info.value.filename == "az://account/container/a.txt"
    assert container.objects["b.txt"] == b"old" and container.deleted == []


def test_a_service_that_does_not_implement_the_copy_is_read_and_written_instead(az):
    backend, container = _container_with(**{"a.txt": b"content"})
    container.copy_errors["a.txt"] = _NotImplemented("501")
    az("az://account/container/a.txt", backend).copy(
        az("az://account/container/b.txt", backend)
    )
    assert container.objects["b.txt"] == b"content"
    assert container.downloads == ["a.txt"] and container.uploads == ["b.txt"]


def test_copy_into_another_container_is_streamed(az):
    backend, container = _container_with(**{"a.txt": b"content"})
    az("az://account/container/a.txt", backend).copy(
        az("az://account/elsewhere/b.txt", backend)
    )
    elsewhere = backend.client_obj.containers["elsewhere"]
    assert elsewhere.objects == {"b.txt": b"content"}
    assert elsewhere.uploads == ["b.txt"]
    assert container.copies == [] and container.downloads == ["a.txt"]
    assert elsewhere.copies == []


def test_copy_between_two_backends_is_streamed(az):
    source_backend, source_container = _container_with(**{"a.txt": b"content"})
    target_backend, target_container = _container_with()
    az("az://account/container/a.txt", source_backend).copy(
        az("az://account/container/b.txt", target_backend)
    )
    assert target_container.objects == {"b.txt": b"content"}
    assert source_container.copies == [] and target_container.copies == []
    assert source_container.downloads == ["a.txt"]


# --- reads and writes stream ------------------------------------------------


def test_open_reads_the_blob_in_ranges_of_one_chunk(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, container = _container_with(**{"big.bin": bytes(range(10))})
    with az("az://account/container/big.bin", backend).open("rb") as stream:
        assert stream.read() == bytes(range(10))
    assert container.ranges == [("big.bin", 0, 4), ("big.bin", 4, 4), ("big.bin", 8, 4)]


def test_a_stream_of_a_large_blob_seeks_by_range(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, _container = _container_with(**{"big.bin": bytes(range(10))})
    with az("az://account/container/big.bin", backend).open("rb") as stream:
        assert stream.seekable()
        stream.seek(6)
        assert stream.read(2) == bytes([6, 7])
        stream.seek(-2, 2)
        assert stream.read() == bytes([8, 9])


class _PlainBackend:
    """A backend that is not a `BaseAZBackend` and sets none of its options."""

    account = "account"

    def __init__(self, client):
        self._client = client

    def client(self):
        return self._client


def test_the_size_of_a_ranged_read_is_an_option_of_the_backend(az):
    data = bytes(range(10))
    chunked, chunked_container = _container_with(**{"big.bin": data})
    chunked.read_chunk = 4
    plain, plain_container = _container_with(**{"big.bin": data})
    default = _PlainBackend(plain.client_obj)
    for backend in (chunked, plain, default):
        assert az("az://account/container/big.bin", backend).read_bytes() == data
    assert chunked_container.ranges == [
        ("big.bin", 0, 4),
        ("big.bin", 4, 4),
        ("big.bin", 8, 4),
    ]
    # A backend that sets nothing reads in the chunk of `BaseAZBackend`.
    assert plain_container.ranges == [
        ("big.bin", 0, BaseAZBackend.read_chunk),
        ("big.bin", 0, BaseAZBackend.read_chunk),
    ]


def test_open_reads_an_empty_blob(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, _container = _container_with(**{"empty": b""})
    assert az("az://account/container/empty", backend).read_bytes() == b""


def test_a_range_that_fails_midway_is_an_oserror(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, _container = _container_with(**{"big.bin": bytes(range(10))})
    original = _FakeBlobClient.download_blob

    def failing(self, offset=None, length=None, **kwargs):
        if offset:
            raise _ServiceUnavailable("503")
        return original(self, offset, length, **kwargs)

    monkeypatch.setattr(_FakeBlobClient, "download_blob", failing)
    with az("az://account/container/big.bin", backend).open("rb") as stream:
        assert stream.read(4) == bytes(range(4))
        with pytest.raises(OSError) as info:
            stream.read(1)
    assert info.value.filename == "az://account/container/big.bin"
    assert info.value.__cause__ is None


def test_r_plus_changes_a_blob_that_spans_several_ranges(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, container = _container_with(**{"big.bin": bytes(range(10))})
    with az("az://account/container/big.bin", backend).open("r+b") as stream:
        stream.seek(5)
        stream.write(b"\xff")
    assert container.objects["big.bin"] == bytes([0, 1, 2, 3, 4, 255, 6, 7, 8, 9])


def test_a_write_that_fits_the_spool_is_one_request_and_a_larger_one_a_stream(
    az,
    monkeypatch,
):
    monkeypatch.setattr(az_module, "SPOOL_SIZE", 8)
    backend, container = _container_with()
    az("az://account/container/small", backend).write_bytes(b"12345678")
    az("az://account/container/large", backend).write_bytes(b"123456789")
    assert container.objects == {"small": b"12345678", "large": b"123456789"}
    assert container.stream_uploads == ["large"]


@pytest.mark.parametrize("size", [3, 9], ids=["one-request", "blocks"])
def test_an_exclusive_upload_does_not_replace_a_blob_created_meanwhile(
    az, monkeypatch, size
):
    monkeypatch.setattr(az_module, "SPOOL_SIZE", 8)
    backend, container = _container_with()
    stream = az("az://account/container/there", backend).open("xb")
    stream.write(b"x" * size)
    # Another writer creates the blob while the stream is open.
    container.objects["there"] = b"keep"
    with pytest.raises(FileExistsError):
        stream.close()
    assert container.objects == {"there": b"keep"}
    assert container.stream_uploads == (["there"] if size > 8 else [])


# --- a read is one version of the blob --------------------------------------

OLD = b"0123456789ab"
NEW = b"ZZZZZZZZZZZZ"


def _change_before_read(container, name, change, *, nth=2):
    """Run `change()` just before the `nth` request for the content of `name`."""
    seen = []

    def hook(read):
        if read == name:
            seen.append(read)
            if len(seen) == nth:
                change()

    container.before_read = hook


def _stale_errno():
    return getattr(errno, "ESTALE", errno.EIO)


def test_a_read_across_an_overwrite_raises_and_returns_no_mixed_bytes(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, container = _container_with(**{"big.bin": OLD})
    _change_before_read(
        container, "big.bin", lambda: container.objects.update(**{"big.bin": NEW})
    )
    path = az("az://account/container/big.bin", backend)
    got = []
    with path.open("rb") as stream:
        got.append(stream.read(4))
        with pytest.raises(ObjectChangedError) as info:
            stream.read(4)
    assert got == [OLD[:4]]
    error = info.value
    assert not isinstance(error, FileNotFoundError)
    assert error.errno == _stale_errno()
    assert "changed while it was being read" in error.strerror
    assert error.filename == "az://account/container/big.bin"
    assert error.__cause__ is None


def test_a_read_of_a_blob_deleted_midway_is_file_not_found(az, monkeypatch):
    for answers_412 in (False, True):
        monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
        backend, container = _container_with(**{"big.bin": OLD})
        container.missing_is_412 = answers_412
        _change_before_read(
            container, "big.bin", lambda: container.objects.pop("big.bin")
        )
        with az("az://account/container/big.bin", backend).open("rb") as stream:
            assert stream.read(4) == OLD[:4]
            with pytest.raises(FileNotFoundError):
                stream.read(4)


def test_a_read_pins_the_etag_of_its_first_request(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, container = _container_with(**{"big.bin": OLD})
    etag = '"%d"' % container.objects.version("big.bin")
    with az("az://account/container/big.bin", backend).open("rb") as stream:
        assert stream.read() == OLD
        stream.seek(5)
        assert stream.read(2) == OLD[5:7]
        stream.seek(-2, 2)
        assert stream.read() == OLD[-2:]
    assert container.reads[0] == ("big.bin", 0, None)
    assert {read[2] for read in container.reads[1:]} == {etag}
    assert ("big.bin", 4, etag) in container.reads


def test_two_opens_are_two_snapshots(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, container = _container_with(**{"big.bin": OLD})
    path = az("az://account/container/big.bin", backend)
    with path.open("rb") as first:
        assert first.read(4) == OLD[:4]
        container.objects["big.bin"] = NEW
        with path.open("rb") as second:
            assert second.read() == NEW
        with pytest.raises(ObjectChangedError) as info:
            first.read()
    assert info.value.errno == _stale_errno()


def test_seeking_to_the_end_uses_the_size_the_first_response_reported(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, container = _container_with(**{"big.bin": OLD})
    asked = []
    original = _FakeBlobClient.get_blob_properties

    def recorded(self, etag=None, match_condition=None):
        asked.append(etag)
        return original(self, etag, match_condition)

    monkeypatch.setattr(_FakeBlobClient, "get_blob_properties", recorded)
    with az("az://account/container/big.bin", backend).open("rb") as stream:
        # A longer blob that replaces it does not change the size being read.
        container.objects["big.bin"] = NEW + NEW
        assert stream.seek(-3, 2) == len(OLD) - 3
        with pytest.raises(ObjectChangedError) as info:
            stream.read()
    assert info.value.errno == _stale_errno()
    # Only the check for a blob that is gone asked, and it names no ETag.
    assert asked == [None]


def test_seeking_to_the_end_measures_the_pinned_blob_when_no_total_was_reported(
    az,
    monkeypatch,
):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    original = _FakeDownloader.__init__

    def without_a_total(self, data, etag='"0"', total=None):
        original(self, data, etag, total)
        self.properties.content_range = None

    monkeypatch.setattr(_FakeDownloader, "__init__", without_a_total)
    backend, container = _container_with(**{"big.bin": OLD})
    asked = []
    measure = _FakeBlobClient.get_blob_properties

    def recorded(self, etag=None, match_condition=None):
        asked.append(etag)
        return measure(self, etag, match_condition)

    monkeypatch.setattr(_FakeBlobClient, "get_blob_properties", recorded)
    etag = '"%d"' % container.objects.version("big.bin")
    with az("az://account/container/big.bin", backend).open("rb") as stream:
        assert stream.seek(-3, 2) == len(OLD) - 3
        assert stream.read() == OLD[-3:]
    assert asked == [etag]
    with az("az://account/container/big.bin", backend).open("rb") as stream:
        container.objects["big.bin"] = NEW + NEW
        with pytest.raises(ObjectChangedError) as info:
            stream.seek(-3, 2)
    assert info.value.errno == _stale_errno()


def test_r_plus_reads_its_initial_content_as_one_version(az, monkeypatch):
    monkeypatch.setattr(_FakeBackend, "read_chunk", 4)
    backend, container = _container_with(**{"big.bin": OLD})
    _change_before_read(
        container, "big.bin", lambda: container.objects.update(**{"big.bin": NEW})
    )
    with pytest.raises(ObjectChangedError) as info:
        az("az://account/container/big.bin", backend).open("r+b")
    assert info.value.errno == _stale_errno()
    assert container.objects["big.bin"] == NEW and container.uploads == []
