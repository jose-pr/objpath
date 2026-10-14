"""Behaviour of `S3Path` and of the `s3:` URI path over it, through a fake
boto3-shaped client (a `BaseS3Backend`) and through moto, no real AWS account.
Covers prefix-emulated directories, the list_objects_v2 paging shape,
copy_object + delete_object rename, copy, checksums and the contract every
`Path` meets.

Every test that has the behaviour of a path runs through the `flavor` fixture:
once on `S3Path` and once on the `S3URIPath` of the same URL.
"""

from __future__ import annotations

import collections
import datetime
import errno
import hashlib
import io

import pytest

botocore = pytest.importorskip("botocore")
import botocore.exceptions as _botoexc

from objpath import BaseS3Backend, S3Backend
from objpath.exceptions import UnreachableKeysError
from pathapi.testing import PathContract
from store_support import make_path

# boto3 announces the end of its Python 3.9 support when it creates its first
# client.
pytestmark = pytest.mark.filterwarnings(r"ignore:.*Python 3\.9.*:Warning:boto3(\..*)?$")


def _client_error(code):
    return _botoexc.ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class _Paginator:
    def __init__(self, client):
        self._client = client

    def paginate(self, **kwargs):
        yield self._client.list_objects_v2(**kwargs)


class _FakeS3Client:
    def __init__(self):
        self.objects = {}  # key -> bytes
        self.head_extra = {}  # key -> fields added to its HeadObject reply
        self.delete_errors = []

    def head_bucket(self, Bucket):
        return {}

    def head_object(self, Bucket, Key, ChecksumMode=None):
        if Key not in self.objects:
            raise _client_error("404")
        data = self.objects[Key]
        return {
            "ContentLength": len(data),
            "LastModified": datetime.datetime(
                2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc
            ),
            "ETag": '"%s"' % hashlib.md5(data).hexdigest(),
            **self.head_extra.get(Key, {}),
        }

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, MaxKeys=None):
        contents = []
        common = set()
        for key in sorted(self.objects):
            if not key.startswith(Prefix):
                continue
            rest = key[len(Prefix) :]
            if Delimiter and Delimiter in rest:
                common.add(Prefix + rest.split(Delimiter, 1)[0] + Delimiter)
            else:
                contents.append(
                    {
                        "Key": key,
                        "Size": len(self.objects[key]),
                        "LastModified": datetime.datetime(
                            2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc
                        ),
                    }
                )
        if MaxKeys:
            contents = contents[:MaxKeys]
        result = {"KeyCount": len(contents) + len(common)}
        if contents:
            result["Contents"] = contents
        if common:
            result["CommonPrefixes"] = [{"Prefix": p} for p in sorted(common)]
        return result

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise _client_error("PreconditionFailed")
        if hasattr(Body, "read"):
            Body = Body.read()
        self.objects[Key] = Body if isinstance(Body, bytes) else bytes(Body)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {"Deleted": Delete["Objects"], "Errors": self.delete_errors}

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Key] = self.objects[CopySource["Key"]]


class _FakeBackend(BaseS3Backend):
    def __init__(self):
        self._client = _FakeS3Client()
        self.asked = 0

    def client(self):
        self.asked += 1
        return self._client


def _s3(flavor, url, backend=None):
    return make_path(flavor, url, backend=backend or _FakeBackend())


def test_bucket_and_key(flavor):
    p = _s3(flavor, "s3://bucket/docs/readme.txt")
    assert p.bucket == "bucket"
    assert p.key == "docs/readme.txt"


def test_stat_file(flavor):
    backend = _FakeBackend()
    backend._client.objects["docs/readme.txt"] = b"hello world"
    st = _s3(flavor, "s3://bucket/docs/readme.txt", backend).stat()
    assert st.st_size == 11
    assert not st.is_dir()


def test_stat_dir_via_prefix(flavor):
    backend = _FakeBackend()
    backend._client.objects["docs/readme.txt"] = b"hello"
    assert _s3(flavor, "s3://bucket/docs", backend).stat().is_dir()


def test_stat_root_is_dir(flavor):
    assert _s3(flavor, "s3://bucket/", _FakeBackend()).stat().is_dir()


def test_stat_missing_raises_file_not_found(flavor):
    with pytest.raises(FileNotFoundError):
        _s3(flavor, "s3://bucket/missing.txt", _FakeBackend()).stat()


def test_listing_the_root_and_a_nested_directory(flavor):
    backend = _FakeBackend()
    backend._client.objects["docs/readme.txt"] = b"x"
    backend._client.objects["docs/sub/file.txt"] = b"y"
    backend._client.objects["top.txt"] = b"z"
    root = _s3(flavor, "s3://bucket/", backend)
    assert sorted(child.name for child in root.iterdir()) == ["docs", "top.txt"]
    docs = _s3(flavor, "s3://bucket/docs", backend)
    assert sorted(child.name for child in docs.iterdir()) == ["readme.txt", "sub"]


def test_read_bytes(flavor):
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"hello"
    assert _s3(flavor, "s3://bucket/a.txt", backend).read_bytes() == b"hello"


def test_read_missing_raises_file_not_found(flavor):
    with pytest.raises(FileNotFoundError):
        _s3(flavor, "s3://bucket/missing.txt", _FakeBackend()).read_bytes()


def test_write_bytes_uploads_via_put_object_on_close(flavor):
    backend = _FakeBackend()
    _s3(flavor, "s3://bucket/new.txt", backend).write_bytes(b"new content")
    assert backend._client.objects["new.txt"] == b"new content"


def test_mkdir_creates_marker_object(flavor):
    backend = _FakeBackend()
    _s3(flavor, "s3://bucket/newdir", backend).mkdir()
    assert "newdir/" in backend._client.objects


def test_mkdir_existing_raises_file_exists(flavor):
    backend = _FakeBackend()
    backend._client.objects["newdir/"] = b""
    with pytest.raises(FileExistsError):
        _s3(flavor, "s3://bucket/newdir", backend).mkdir()


def test_unlink_deletes_object(flavor):
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"x"
    _s3(flavor, "s3://bucket/a.txt", backend).unlink()
    assert "a.txt" not in backend._client.objects


def test_unlink_missing_without_missing_ok_raises(flavor):
    with pytest.raises(FileNotFoundError):
        _s3(flavor, "s3://bucket/missing.txt", _FakeBackend()).unlink()


def test_unlink_missing_ok(flavor):
    _s3(flavor, "s3://bucket/missing.txt", _FakeBackend()).unlink(missing_ok=True)


def test_rmdir_empty_deletes_marker(flavor):
    backend = _FakeBackend()
    backend._client.objects["dir/"] = b""
    _s3(flavor, "s3://bucket/dir", backend).rmdir()
    assert "dir/" not in backend._client.objects


def test_rmdir_non_empty_raises_oserror(flavor):
    backend = _FakeBackend()
    backend._client.objects["dir/"] = b""
    backend._client.objects["dir/file.txt"] = b"x"
    with pytest.raises(OSError):
        _s3(flavor, "s3://bucket/dir", backend).rmdir()


def test_rm_recursive_uses_batch_delete_for_prefix(flavor):
    backend = _FakeBackend()
    backend._client.objects.update(
        {
            "dir/": b"",
            "dir/a.txt": b"a",
            "dir/sub/b.txt": b"b",
            "other.txt": b"keep",
        }
    )
    _s3(flavor, "s3://bucket/dir", backend).rm(recursive=True)
    assert backend._client.objects == {"other.txt": b"keep"}


def test_rm_recursive_deletes_exact_object_key(flavor):
    backend = _FakeBackend()
    backend._client.objects.update(
        {
            "file.txt": b"x",
            "file.txt/nested.txt": b"keep",
        }
    )
    _s3(flavor, "s3://bucket/file.txt", backend).rm(recursive=True)
    assert backend._client.objects == {"file.txt/nested.txt": b"keep"}


def test_rm_recursive_missing_ok(flavor):
    _s3(flavor, "s3://bucket/missing", _FakeBackend()).rm(
        recursive=True, missing_ok=True
    )


def test_rm_recursive_missing_without_missing_ok_raises(flavor):
    with pytest.raises(FileNotFoundError):
        _s3(flavor, "s3://bucket/missing", _FakeBackend()).rm(recursive=True)


def test_rm_recursive_ignore_error_swallows_missing(flavor):
    calls = []
    _s3(flavor, "s3://bucket/missing", _FakeBackend()).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(FileNotFoundError, "missing")]


def test_rm_recursive_root_guard(flavor):
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"x"
    with pytest.raises(PermissionError):
        _s3(flavor, "s3://bucket/", backend).rm(recursive=True)
    assert backend._client.objects == {"a.txt": b"x"}


def test_rm_recursive_delete_objects_errors_raise(flavor):
    backend = _FakeBackend()
    backend._client.objects["dir/a.txt"] = b"x"
    backend._client.delete_errors = [{"Key": "dir/a.txt", "Code": "AccessDenied"}]
    with pytest.raises(OSError, match="delete_objects failed"):
        _s3(flavor, "s3://bucket/dir", backend).rm(recursive=True)


def test_rm_recursive_delete_objects_errors_ignore_error(flavor):
    backend = _FakeBackend()
    backend._client.objects["dir/a.txt"] = b"x"
    backend._client.delete_errors = [{"Key": "dir/a.txt", "Code": "AccessDenied"}]
    calls = []
    _s3(flavor, "s3://bucket/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(PermissionError, "dir/a.txt")]


def test_rm_non_recursive_keeps_rmdir_contract(flavor):
    backend = _FakeBackend()
    backend._client.objects["dir/file.txt"] = b"x"
    with pytest.raises(OSError):
        _s3(flavor, "s3://bucket/dir", backend).rm()


def test_rename_uses_copy_then_delete(flavor):
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"content"
    _s3(flavor, "s3://bucket/a.txt", backend).rename("b.txt")
    assert backend._client.objects.get("b.txt") == b"content"
    assert "a.txt" not in backend._client.objects


def test_chmod_not_implemented(flavor):
    with pytest.raises(NotImplementedError):
        _s3(flavor, "s3://bucket/a.txt").chmod(0o644)


# --- destination keys are names, not URI syntax -------------------------------
# "?" and "#" are legal S3 key characters: a destination key is not cut at
# either one.


@pytest.mark.parametrize("name", ["b?x.txt", "b#x.txt", "b%20x.txt"])
def test_rename_str_destination_key_is_literal(flavor, name):
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"content"
    _s3(flavor, "s3://bucket/a.txt", backend).rename(name)
    assert backend._client.objects.get(name) == b"content"
    assert "a.txt" not in backend._client.objects


# --- a trailing "/" names the directory, not the "dir/" marker object -------
# `s3://bucket/dir/` is how `aws s3 ls` and the console spell a folder: the
# `dir/` marker is not read as a file, and `rm(recursive=True)` removes the
# whole prefix with it.


def test_trailing_slash_marker_dir_is_a_directory_fake_client(flavor):
    backend = _FakeBackend()
    backend._client.objects.update(
        {"dir/": b"", "dir/a.txt": b"a", "dir/sub/b.txt": b"b", "other": b"k"}
    )
    p = _s3(flavor, "s3://bucket/dir/", backend)
    assert p.is_dir()
    assert not p.is_file()
    p.rm(recursive=True)
    assert backend._client.objects == {"other": b"k"}


def test_rename_to_trailing_slash_destination_drops_the_slash(flavor):
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"content"
    _s3(flavor, "s3://bucket/a.txt", backend).rename(
        _s3(flavor, "s3://bucket/b.txt/", backend)
    )
    assert backend._client.objects == {"b.txt": b"content"}


@pytest.fixture
def moto_s3(aws_test_credentials):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")

    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        yield client


def _moto_keys(client):
    listing = client.list_objects_v2(Bucket="bkt").get("Contents", [])
    return sorted(obj["Key"] for obj in listing)


def test_stat_and_listing_report_the_last_modified_time_as_an_epoch(flavor):
    # The stores answer with timezone-aware UTC times; a naive reading would
    # shift the epoch by the local offset.
    backend = _FakeBackend()
    backend._client.objects["dir/a.txt"] = b"abc"
    assert _s3(flavor, "s3://bucket/dir/a.txt", backend).stat().st_mtime == 1767268800
    entries = dict(_s3(flavor, "s3://bucket/dir", backend)._scandir())
    assert entries["a.txt"].st_mtime == 1767268800
    assert entries["a.txt"].st_size == 3


def test_stat_and_listing_report_the_stored_time_moto(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="dir/a.txt", Body=b"abc")
    stored = int(
        moto_s3.head_object(Bucket="bkt", Key="dir/a.txt")["LastModified"].timestamp()
    )
    assert stored > 1_500_000_000
    assert make_path(flavor, "s3://bkt/dir/a.txt").stat().st_mtime == stored
    entries = dict(make_path(flavor, "s3://bkt/dir")._scandir())
    assert entries["a.txt"].st_mtime == stored


def test_children_carry_the_stat_of_the_listing(flavor):
    # A child's stat() is the listing's own entry, not another request.
    backend = _FakeBackend()
    backend._client.objects["dir/a.txt"] = b"abc"
    children = list(_s3(flavor, "s3://bucket/dir", backend).iterdir())
    asked = backend.asked
    assert [child.stat().st_size for child in children] == [3]
    assert backend.asked == asked


def test_trailing_slash_dir_with_marker_moto(flavor, moto_s3):
    for key in ("dir/", "dir/a.txt", "dir/sub/b.txt", "keep.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x" if "." in key else b"")

    p = make_path(flavor, "s3://bkt/dir/")
    assert p.key == "dir"
    assert p.exists()
    assert p.is_dir()
    assert not p.is_file()
    assert sorted(child.name for child in p.iterdir()) == ["a.txt", "sub"]

    p.rm(recursive=True)
    assert _moto_keys(moto_s3) == ["keep.txt"]


def test_trailing_slash_dir_without_marker_moto(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="plain/x.txt", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="keep.txt", Body=b"k")

    p = make_path(flavor, "s3://bkt/plain/")
    assert p.exists()
    assert p.is_dir()
    assert [child.name for child in p.iterdir()] == ["x.txt"]
    p.rm(recursive=True)
    assert _moto_keys(moto_s3) == ["keep.txt"]


def test_trailing_slash_mkdir_then_rmdir_moto(flavor, moto_s3):
    p = make_path(flavor, "s3://bkt/new/")
    p.mkdir()
    # One "new/" marker, not "new//".
    assert _moto_keys(moto_s3) == ["new/"]
    assert p.is_dir()
    p.rmdir()
    assert _moto_keys(moto_s3) == []


# --- a key that is both an object and a prefix lists as the object -----------


def test_key_that_is_object_and_prefix_lists_as_the_object(flavor, moto_s3):
    from pathapi.mempath import MemPath

    moto_s3.put_object(Bucket="bkt", Key="src/logs", Body=b"FILE-CONTENT")
    moto_s3.put_object(Bucket="bkt", Key="src/logs/2026.txt", Body=b"child")
    moto_s3.put_object(Bucket="bkt", Key="src/other/x.txt", Body=b"x")
    src = make_path(flavor, "s3://bkt/src")
    listing = dict(src._scandir())
    assert sorted(listing) == ["logs", "other"]
    # Agrees with stat()'s exact-object precedence.
    assert not listing["logs"].is_dir()
    assert listing["logs"].st_size == len(b"FILE-CONTENT")
    assert not make_path(flavor, "s3://bkt/src/logs").stat().is_dir()
    # The subtree under the object is not listed, so a recursive copy of the
    # prefix is refused instead of leaving it behind.
    dst = MemPath("/dst")
    with pytest.raises(UnreachableKeysError, match="nothing was changed"):
        src.copy(dst, recursive=True)
    assert not dst.exists()


# --- open("rb") hands out a read-only stream -----------------------------------


def test_open_rb_is_read_only_and_rplus_writes_land(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="f.txt", Body=b"F")
    p = make_path(flavor, "s3://bkt/f.txt")
    with p.open("rb") as f:
        assert not f.writable()
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"zz")
    with p.open("r+b") as f:
        f.write(b"N")
    assert p.read_bytes() == b"N"


# --- a listed name is one component inside the directory that listed it ------


class _PageClient:
    """A client whose one listing page says exactly what the test hands it."""

    def __init__(self, contents=(), prefixes=()):
        self._page = {
            "Contents": [{"Key": key} for key in contents],
            "CommonPrefixes": [{"Prefix": prefix} for prefix in prefixes],
        }

    def get_paginator(self, name):
        client = self

        class _Pages:
            def paginate(self, **kwargs):
                yield client._page

        return _Pages()


class _PageBackend(BaseS3Backend):
    def __init__(self, client):
        self._client = client

    def client(self):
        return self._client


def test_listing_skips_names_that_are_not_one_component(flavor):
    client = _PageClient(
        contents=["dir/ok.txt", "dir/..", "dir/.", "dir/a/b", "dir/", "dir/x\0y"],
        prefixes=["dir/sub/", "dir/../", "dir/./", "dir/p/q/", "dir//"],
    )
    listing = dict(_s3(flavor, "s3://bucket/dir", _PageBackend(client))._scandir())
    assert sorted(listing) == ["ok.txt", "sub"]
    assert listing["sub"].is_dir()


def test_directory_holding_only_unsafe_names_is_still_a_directory(flavor):
    client = _PageClient(contents=["dir/.."])
    path = _s3(flavor, "s3://bucket/dir", _PageBackend(client))
    assert list(path._scandir()) == []


def test_moto_listing_hides_dot_segment_keys(flavor, moto_s3):
    for key in ("u/alice/ok.txt", "u/alice/../x.txt", "u/alice/./y", "u/alice/sub/z"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x")
    alice = make_path(flavor, "s3://bkt/u/alice")
    assert sorted(name for name, _ in alice._scandir()) == ["ok.txt", "sub"]
    assert sorted(child.name for child in alice.iterdir()) == ["ok.txt", "sub"]


_DOT_KEYS = (
    "top.txt",
    "u/alice/ok.txt",
    "u/alice/../x.txt",
    "u/alice/./y.txt",
    "u/alice/sub/z.txt",
    "u/bob/private.txt",
    "u/carol/data.bin",
)


def test_walk_of_a_prefix_with_dot_segment_keys_stays_inside_it(flavor, moto_s3):
    import itertools

    for key in _DOT_KEYS:
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x")
    walked = itertools.islice(make_path(flavor, "s3://bkt/u/alice").walk(), 20)
    assert [(str(path), dirs, files) for path, dirs, files in walked] == [
        ("s3://bkt/u/alice", ["sub"], ["ok.txt"]),
        ("s3://bkt/u/alice/sub", [], ["z.txt"]),
    ]


def test_sync_remove_missing_leaves_the_neighbours_of_a_dot_segment_key(
    flavor, moto_s3
):
    from pathapi.mempath import MemPath
    from pathapi.sync import PathSyncer, SyncEvent

    for key in _DOT_KEYS:
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x")
    source = MemPath("/src")
    source.mkdir()
    (source / "ok.txt").write_text("x")
    removed = []
    syncer = PathSyncer(
        remove_missing=True,
        quick_check=False,
        hook=lambda src, tgt, event, dry: (
            removed.append(str(tgt.path)) if event is SyncEvent.RemovedMissing else None
        ),
    )
    syncer.sync(source, make_path(flavor, "s3://bkt/u/alice"))
    assert removed == ["s3://bkt/u/alice/sub"]
    outside = [key for key in _moto_keys(moto_s3) if not key.startswith("u/alice/")]
    assert outside == ["top.txt", "u/bob/private.txt", "u/carol/data.bin"]


def test_sync_from_a_prefix_with_dot_segment_keys_copies_only_its_own_tree(
    flavor, moto_s3
):
    from pathapi.mempath import MemPath
    from pathapi.sync import PathSyncer, SyncEvent

    for key in _DOT_KEYS:
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x")
    target = MemPath("/mirror")
    target.mkdir()
    copied = []

    class _Runaway(BaseException):
        pass

    def hook(src, tgt, event, dry):
        if event is SyncEvent.Copy:
            copied.append(str(src.path))
            if len(copied) > 20:
                raise _Runaway

    PathSyncer(hook=hook).sync(make_path(flavor, "s3://bkt/u/alice"), target)
    assert sorted(copied) == ["s3://bkt/u/alice/ok.txt", "s3://bkt/u/alice/sub/z.txt"]
    assert sorted(p.name for p in target.iterdir()) == ["ok.txt", "sub"]


# --- a prefix is moved, copied and removed as the same set of keys --------------

_ODD_PREFIXES = {
    "an-empty-segment": ["d/x.txt", "d//y.txt", "d/sub//z.txt"],
    "an-object-that-is-also-a-prefix": [
        "d/x.txt",
        "d/logs",
        "d/logs/2026.txt",
        "d/logs/deep/z.txt",
    ],
    "a-slash-key-that-holds-data": ["d/x.txt", "d/blob/"],
    "a-dot-segment": ["d/x.txt", "d/sub/../z.txt"],
}


def _put_all(client, keys):
    for key in keys:
        empty = key.endswith("/") and key != "d/blob/"
        client.put_object(Bucket="bkt", Key=key, Body=b"" if empty else key.encode())
    client.put_object(Bucket="bkt", Key="unrelated.txt", Body=b"keep")


@pytest.fixture(params=list(_ODD_PREFIXES))
def odd_prefix(request, moto_s3):
    keys = _ODD_PREFIXES[request.param]
    _put_all(moto_s3, keys)
    return sorted(keys + ["unrelated.txt"])


def test_move_of_a_prefix_with_keys_a_listing_hides_keeps_every_key(
    flavor, moto_s3, odd_prefix
):
    with pytest.raises(UnreachableKeysError, match="nothing was changed"):
        make_path(flavor, "s3://bkt/d").move(make_path(flavor, "s3://bkt/moved"))
    assert _moto_keys(moto_s3) == odd_prefix


def test_move_of_such_a_prefix_to_memory_creates_nothing_there(
    flavor, moto_s3, odd_prefix
):
    from pathapi.mempath import MemPath

    target = MemPath("/moved")
    with pytest.raises(UnreachableKeysError):
        make_path(flavor, "s3://bkt/d").move(target)
    assert not target.exists()
    assert _moto_keys(moto_s3) == odd_prefix


def test_rm_recursive_of_such_a_prefix_removes_nothing(flavor, moto_s3, odd_prefix):
    with pytest.raises(UnreachableKeysError, match="nothing was changed"):
        make_path(flavor, "s3://bkt/d").rm(recursive=True)
    assert _moto_keys(moto_s3) == odd_prefix


def test_copy_recursive_of_such_a_prefix_is_refused_before_anything_is_written(
    flavor, moto_s3, odd_prefix
):
    with pytest.raises(UnreachableKeysError, match="nothing was changed") as raised:
        make_path(flavor, "s3://bkt/d").copy(
            make_path(flavor, "s3://bkt/copied"), recursive=True
        )
    assert raised.value.errno == errno.EINVAL
    assert _moto_keys(moto_s3) == odd_prefix


def test_the_refusal_names_the_keys_the_listing_does_not_show(flavor, moto_s3):
    _put_all(moto_s3, ["d/x.txt", "d//y.txt", "d/logs", "d/logs/2026.txt"])
    with pytest.raises(UnreachableKeysError) as raised:
        make_path(flavor, "s3://bkt/d").rm(recursive=True)
    message = str(raised.value)
    assert "'d//y.txt'" in message
    assert "'d/logs/2026.txt'" in message
    assert "'d/x.txt'" not in message


def test_rm_recursive_with_ignore_error_removes_only_the_keys_a_listing_shows(
    flavor, moto_s3
):
    _put_all(
        moto_s3, ["d/", "d/x.txt", "d/sub/a.txt", "d//y.txt", "d/logs", "d/logs/z"]
    )
    seen = []
    make_path(flavor, "s3://bkt/d").rm(
        recursive=True, ignore_error=lambda error, path: seen.append(error) or True
    )
    assert len(seen) == 1 and "'d//y.txt'" in str(seen[0])
    assert isinstance(seen[0], UnreachableKeysError)
    assert _moto_keys(moto_s3) == ["d//y.txt", "d/logs/z", "unrelated.txt"]


def test_removing_the_blocking_object_lets_the_prefix_be_moved(flavor, moto_s3):
    _put_all(moto_s3, ["d/x.txt", "d/logs", "d/logs/2026.txt"])
    make_path(flavor, "s3://bkt/d/logs").unlink()
    make_path(flavor, "s3://bkt/d").move(make_path(flavor, "s3://bkt/moved"))
    assert _moto_keys(moto_s3) == [
        "moved/",
        "moved/logs/",
        "moved/logs/2026.txt",
        "moved/x.txt",
        "unrelated.txt",
    ]


def test_move_of_a_prefix_a_listing_shows_whole_moves_every_key(flavor, moto_s3):
    _put_all(moto_s3, ["d/", "d/x.txt", "d/sub/a.txt", "d/sub/deep/b.txt", "d/empty/"])
    make_path(flavor, "s3://bkt/d").move(make_path(flavor, "s3://bkt/moved"))
    assert _moto_keys(moto_s3) == [
        "moved/",
        "moved/empty/",
        "moved/sub/",
        "moved/sub/a.txt",
        "moved/sub/deep/",
        "moved/sub/deep/b.txt",
        "moved/x.txt",
        "unrelated.txt",
    ]


def _counting_backend():
    class Counting(S3Backend):
        __slots__ = ("calls",)

        def client(self):
            first = self._client is None
            client = super().client()
            if first:
                self.calls = []
                original = client._make_api_call

                def counted(operation, params):
                    self.calls.append(operation)
                    return original(operation, params)

                client._make_api_call = counted
            return client

    return Counting()


def _keyed_backend():
    """An `S3Backend` that records `(operation, key or prefix)` per request."""

    class Keyed(S3Backend):
        __slots__ = ("calls",)

        def client(self):
            first = self._client is None
            client = super().client()
            if first:
                self.calls = []
                original = client._make_api_call

                def recorded(operation, params):
                    self.calls.append(
                        (operation, params.get("Key", params.get("Prefix")))
                    )
                    return original(operation, params)

                client._make_api_call = recorded
            return client

    return Keyed()


_STAT_REQUESTS = ("HeadObject", "ListObjectsV2")


def test_move_of_a_prefix_asks_about_the_new_directory_only_as_often_as_it_must(
    flavor, moto_s3
):
    _put_all(moto_s3, ["d/", "d/x.txt", "d/sub/a.txt", "d/empty/"])
    backend = _keyed_backend()

    make_path(flavor, "s3://bkt/d", backend=backend).move(
        make_path(flavor, "s3://bkt/moved", backend=backend)
    )

    calls = collections.Counter(backend.calls)
    reads = calls["HeadObject", "moved"], sum(
        n for (operation, _key), n in calls.items() if operation in _STAT_REQUESTS
    )
    # The target is stat-ed once by move() and once by the creation of the
    # directory; the other stat requests are bounded by the entries moved.
    assert reads[0] <= 2
    assert reads[1] <= 31
    assert _moto_keys(moto_s3) == [
        "moved/",
        "moved/empty/",
        "moved/sub/",
        "moved/sub/a.txt",
        "moved/x.txt",
        "unrelated.txt",
    ]


def test_rm_recursive_of_a_prefix_a_listing_shows_is_one_batched_delete(
    flavor, moto_s3
):
    _put_all(moto_s3, ["d/", "d/x.txt", "d/sub/a.txt", "d/sub/deep/b.txt", "d/empty/"])
    backend = _counting_backend()
    make_path(flavor, "s3://bkt/d", backend=backend).rm(recursive=True)
    assert backend.calls.count("DeleteObjects") == 1
    assert "DeleteObject" not in backend.calls
    assert _moto_keys(moto_s3) == ["unrelated.txt"]


# --- a write or rename never turns a prefix directory into an object -----------


def test_write_onto_a_prefix_directory_is_refused(flavor, moto_s3):
    _put_all(moto_s3, ["d/", "d/x.txt"])
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt/d").write_bytes(b"clobber")
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt/d").write_text("clobber")
    assert _moto_keys(moto_s3) == ["d/", "d/x.txt", "unrelated.txt"]


def test_rename_onto_a_prefix_directory_is_refused(flavor, moto_s3):
    _put_all(moto_s3, ["b.txt", "dst/keep.txt"])
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt/b.txt").rename("dst")
    assert _moto_keys(moto_s3) == ["b.txt", "dst/keep.txt", "unrelated.txt"]


def test_write_and_rename_onto_an_object_replace_it(flavor, moto_s3):
    _put_all(moto_s3, ["a.txt", "b.txt"])
    make_path(flavor, "s3://bkt/a.txt").write_bytes(b"new")
    assert moto_s3.get_object(Bucket="bkt", Key="a.txt")["Body"].read() == b"new"
    make_path(flavor, "s3://bkt/b.txt").rename("a.txt")
    assert moto_s3.get_object(Bucket="bkt", Key="a.txt")["Body"].read() == b"b.txt"
    assert _moto_keys(moto_s3) == ["a.txt", "unrelated.txt"]


def test_rename_to_another_bucket_is_not_implemented_so_move_copies(flavor, moto_s3):
    moto_s3.create_bucket(Bucket="other")
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"payload")
    source = make_path(flavor, "s3://bkt/a.txt")
    target = make_path(flavor, "s3://other/b.txt")
    with pytest.raises(NotImplementedError):
        source.rename(target)
    assert _moto_keys(moto_s3) == ["a.txt"]
    source.move(target)
    assert _stored(moto_s3, "b.txt", "other") == b"payload"
    assert _moto_keys(moto_s3) == []


def _failing_backend(operation, code):
    backend = S3Backend()
    client = backend.client()
    original = client._make_api_call

    def failing(name, params):
        if name == operation:
            raise _client_error(code)
        return original(name, params)

    client._make_api_call = failing
    return backend


def test_write_where_the_prefix_cannot_be_listed_writes(flavor, moto_s3):
    backend = _failing_backend("ListObjectsV2", "AccessDenied")
    make_path(flavor, "s3://bkt/w.txt", backend=backend).write_bytes(b"written")
    assert _moto_keys(moto_s3) == ["w.txt"]


# --- mkdir treats only "not found" as absent ------------------------------------


@pytest.mark.parametrize("code", ["503", "AccessDenied"])
def test_mkdir_does_not_create_a_marker_beside_an_object_when_the_probe_fails(
    flavor, moto_s3, code
):
    moto_s3.put_object(Bucket="bkt", Key="existing.txt", Body=b"keep")
    backend = _failing_backend("HeadObject", code)
    with pytest.raises(OSError):
        make_path(flavor, "s3://bkt/existing.txt", backend=backend).mkdir()
    assert _moto_keys(moto_s3) == ["existing.txt"]


def test_mkdir_does_not_create_a_marker_when_the_prefix_probe_fails(flavor, moto_s3):
    backend = _failing_backend("ListObjectsV2", "503")
    with pytest.raises(OSError):
        make_path(flavor, "s3://bkt/newdir", backend=backend).mkdir()
    assert _moto_keys(moto_s3) == []


def test_mkdir_on_an_existing_object_is_file_exists(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="existing.txt", Body=b"keep")
    with pytest.raises(FileExistsError):
        make_path(flavor, "s3://bkt/existing.txt").mkdir()
    assert _moto_keys(moto_s3) == ["existing.txt"]


def test_mkdir_of_a_missing_path_writes_the_marker(flavor, moto_s3):
    make_path(flavor, "s3://bkt/newdir").mkdir()
    assert _moto_keys(moto_s3) == ["newdir/"]


# --- listing past one page, and the requests an operation sends -----------------


def _recording_backend(max_keys=None):
    """A backend whose client records the name of every request it sends in
    `sent`; with `max_keys`, each listing page holds at most that many keys."""
    backend = S3Backend()
    sent = []
    events = backend.client().meta.events
    events.register("before-call.s3", lambda model, **_: sent.append(model.name))
    if max_keys is not None:

        def small_pages(params, **_):
            params["MaxKeys"] = max_keys

        events.register("before-parameter-build.s3.ListObjectsV2", small_pages)
    return backend, sent


def test_a_listing_that_spans_pages_shows_every_entry_once(flavor, moto_s3):
    for index in range(3):
        moto_s3.put_object(Bucket="bkt", Key=f"paged/d{index}/x", Body=b"x")
        moto_s3.put_object(Bucket="bkt", Key=f"paged/f{index}", Body=b"x")
    backend, sent = _recording_backend(max_keys=2)
    names = sorted(
        child.name
        for child in make_path(flavor, "s3://bkt/paged", backend=backend).iterdir()
    )
    assert names == ["d0", "d1", "d2", "f0", "f1", "f2"]
    assert sent.count("ListObjectsV2") == 3


def test_removing_a_prefix_lists_every_page(flavor, moto_s3):
    for index in range(7):
        moto_s3.put_object(Bucket="bkt", Key=f"paged/f{index}", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="keep.txt", Body=b"x")
    backend, sent = _recording_backend(max_keys=2)
    make_path(flavor, "s3://bkt/paged", backend=backend).rm(recursive=True)
    assert _moto_keys(moto_s3) == ["keep.txt"]
    assert sent.count("ListObjectsV2") == 4
    assert sent.count("DeleteObjects") == 1


# --- checksum(): the digest the store keeps ---------------------------------------


def _multipart_object(client, key, parts=2):
    upload = client.create_multipart_upload(Bucket="bkt", Key=key)
    done = []
    for number in range(1, parts + 1):
        reply = client.upload_part(
            Bucket="bkt",
            Key=key,
            UploadId=upload["UploadId"],
            PartNumber=number,
            Body=bytes([number]) * (5 * 1024**2),
        )
        done.append({"ETag": reply["ETag"], "PartNumber": number})
    client.complete_multipart_upload(
        Bucket="bkt",
        Key=key,
        UploadId=upload["UploadId"],
        MultipartUpload={"Parts": done},
    )


def test_checksum_of_a_single_part_object_is_its_md5_and_reads_no_byte(flavor, moto_s3):
    content = bytes(range(256)) * 40
    moto_s3.put_object(Bucket="bkt", Key="d/a.bin", Body=content)
    backend, sent = _recording_backend()
    path = make_path(flavor, "s3://bkt/d/a.bin", backend=backend)
    assert path.checksum("md5") == hashlib.md5(content).hexdigest()
    assert path.checksum() == hashlib.md5(content).hexdigest()
    assert sent == ["HeadObject", "HeadObject"]


def test_checksum_refuses_the_etag_of_a_multipart_object(flavor, moto_s3):
    from pathapi import checksum as checksums

    _multipart_object(moto_s3, "big.bin")
    etag = moto_s3.head_object(Bucket="bkt", Key="big.bin")["ETag"]
    assert etag.endswith('-2"')
    path = make_path(flavor, "s3://bkt/big.bin")
    with pytest.raises(NotImplementedError):
        path.checksum("md5")
    assert checksums.native(path, "md5") is None


def test_checksum_refuses_the_etag_of_a_kms_encrypted_object(flavor, moto_s3):
    moto_s3.put_object(
        Bucket="bkt", Key="k.bin", Body=b"secret", ServerSideEncryption="aws:kms"
    )
    with pytest.raises(NotImplementedError):
        make_path(flavor, "s3://bkt/k.bin").checksum("md5")
    moto_s3.put_object(
        Bucket="bkt", Key="s.bin", Body=b"secret", ServerSideEncryption="AES256"
    )
    assert (
        make_path(flavor, "s3://bkt/s.bin").checksum("md5")
        == hashlib.md5(b"secret").hexdigest()
    )


def test_checksum_offers_a_full_object_checksum_the_store_keeps(flavor, moto_s3):
    import zlib

    content = b"the whole content"
    moto_s3.put_object(
        Bucket="bkt", Key="sha.bin", Body=content, ChecksumAlgorithm="SHA256"
    )
    moto_s3.put_object(
        Bucket="bkt", Key="crc.bin", Body=content, ChecksumAlgorithm="CRC32"
    )
    sha = make_path(flavor, "s3://bkt/sha.bin")
    crc = make_path(flavor, "s3://bkt/crc.bin")
    assert sha.checksum("sha256") == hashlib.sha256(content).hexdigest()
    assert crc.checksum("crc32") == "%08x" % zlib.crc32(content)
    for path, other in ((sha, "crc32"), (crc, "sha256"), (crc, "sha512")):
        with pytest.raises(NotImplementedError):
            path.checksum(other)


def test_checksum_refuses_a_composite_checksum(flavor):
    sha256 = "LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ="
    backend = _FakeBackend()
    objects, extra = backend._client.objects, backend._client.head_extra
    objects.update({"multi": b"hello", "typed": b"hello", "whole": b"hello"})
    extra["multi"] = {"ChecksumSHA256": sha256 + "-2"}
    extra["typed"] = {"ChecksumSHA256": sha256, "ChecksumType": "COMPOSITE"}
    extra["whole"] = {"ChecksumSHA256": sha256, "ChecksumType": "FULL_OBJECT"}
    for key in ("multi", "typed"):
        with pytest.raises(NotImplementedError):
            _s3(flavor, f"s3://bucket/{key}", backend).checksum("sha256")
    whole = _s3(flavor, "s3://bucket/whole", backend)
    assert whole.checksum("sha256") == hashlib.sha256(b"hello").hexdigest()


def test_checksum_of_a_missing_path_a_directory_and_the_bucket(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="d/a.txt", Body=b"x")
    with pytest.raises(FileNotFoundError):
        make_path(flavor, "s3://bkt/nothing").checksum()
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt/d").checksum()
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt").checksum()


def test_supported_checksums_asks_the_store_nothing(flavor):
    backend = _FakeBackend()
    path = _s3(flavor, "s3://bucket/a", backend)
    assert path.supported_checksums() == frozenset({"md5"})
    assert backend.asked == 0


def test_syncing_an_unchanged_tree_to_another_prefix_reads_no_object(flavor, moto_s3):
    from pathapi.sync import PathSyncer

    for index in range(100):
        moto_s3.put_object(
            Bucket="bkt",
            Key=f"src/d{index % 5}/f{index:03d}.bin",
            Body=bytes([index]) * (index + 1),
        )
    backend, sent = _recording_backend()
    syncer = PathSyncer(quick_check=False)
    source = make_path(flavor, "s3://bkt/src", backend=backend)
    target = make_path(flavor, "s3://bkt/dst", backend=backend)
    syncer.sync(source, target)
    assert len([key for key in _moto_keys(moto_s3) if key.startswith("dst/")]) > 100
    del sent[:]
    syncer.sync(source, target)
    assert sent.count("GetObject") == 0
    assert not {"PutObject", "CopyObject", "DeleteObject"} & set(sent)
    # A changed object is found and copied.
    moto_s3.put_object(Bucket="bkt", Key="src/d0/f000.bin", Body=b"changed")
    syncer.sync(source, target)
    changed = make_path(flavor, "s3://bkt/dst/d0/f000.bin", backend=backend)
    assert changed.read_bytes() == b"changed"


# --- copy(): one copy request in the store ----------------------------------------


def _refusing_copy_backend(code, only_key=None):
    """A recording backend whose CopyObject is answered with the error `code`
    (for `only_key` alone, when given); the refused calls are in `attempts`."""
    backend, sent = _recording_backend()
    client = backend.client()
    original = client._make_api_call
    attempts = []

    def refusing(name, params):
        if name == "CopyObject" and only_key in (None, params["Key"]):
            attempts.append(params["Key"])
            raise _client_error(code)
        return original(name, params)

    client._make_api_call = refusing
    return backend, sent, attempts


def _stored(client, key, bucket="bkt"):
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def test_copy_of_a_file_within_a_bucket_is_one_copy_request_and_reads_nothing(
    flavor, moto_s3
):
    moto_s3.put_object(
        Bucket="bkt",
        Key="d/a.bin",
        Body=b"payload",
        ContentType="text/x-demo",
        Metadata={"owner": "me"},
    )
    backend, sent = _recording_backend()
    make_path(flavor, "s3://bkt/d/a.bin", backend=backend).copy(
        make_path(flavor, "s3://bkt/d/b.bin", backend=backend)
    )
    assert _stored(moto_s3, "d/b.bin") == b"payload"
    assert sent.count("CopyObject") == 1
    assert not {"GetObject", "PutObject", "DeleteObject"} & set(sent)
    # The store copies the object's type and metadata along with its bytes.
    head = moto_s3.head_object(Bucket="bkt", Key="d/b.bin")
    assert head["ContentType"] == "text/x-demo" and head["Metadata"] == {"owner": "me"}


def test_copy_of_a_file_between_two_buckets_of_one_client_is_one_copy_request(
    flavor, moto_s3
):
    moto_s3.create_bucket(Bucket="other")
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    backend, sent = _recording_backend()
    make_path(flavor, "s3://bkt/a.bin", backend=backend).copy(
        make_path(flavor, "s3://other/dir/a.bin", backend=backend)
    )
    assert _stored(moto_s3, "dir/a.bin", "other") == b"payload"
    assert sent.count("CopyObject") == 1
    assert not {"GetObject", "PutObject"} & set(sent)


def test_copy_of_a_file_named_by_a_string_is_one_copy_request(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    backend, sent = _recording_backend()
    make_path(flavor, "s3://bkt/a.bin", backend=backend).copy("b.bin")
    assert _stored(moto_s3, "b.bin") == b"payload"
    assert sent.count("CopyObject") == 1 and "GetObject" not in sent


def test_copy_reports_progress_once_with_the_total(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"x" * 1234)
    moto_s3.put_object(Bucket="bkt", Key="empty", Body=b"")
    backend, _sent = _recording_backend()
    calls = []
    for name, size in (("a.bin", 1234), ("empty", 0)):
        source = make_path(flavor, f"s3://bkt/{name}", backend=backend)
        source.copy(
            make_path(flavor, f"s3://bkt/copy-of-{name}", backend=backend),
            progress=lambda path, copied, total: calls.append(
                (str(path), copied, total)
            ),
        )
        assert calls == [(f"s3://bkt/{name}", size, size)]
        del calls[:]


def test_copy_onto_an_existing_object_needs_overwrite_and_deletes_nothing_first(
    flavor, moto_s3
):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"new")
    moto_s3.put_object(Bucket="bkt", Key="b.bin", Body=b"old")
    backend, sent = _recording_backend()
    source = make_path(flavor, "s3://bkt/a.bin", backend=backend)
    target = make_path(flavor, "s3://bkt/b.bin", backend=backend)
    with pytest.raises(FileExistsError):
        source.copy(target)
    assert "CopyObject" not in sent and _stored(moto_s3, "b.bin") == b"old"
    source.copy(target, overwrite=True)
    assert _stored(moto_s3, "b.bin") == b"new"
    assert sent.count("CopyObject") == 1 and "DeleteObject" not in sent


def test_copy_onto_itself_is_refused_before_the_store_is_asked(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"keep")
    backend, sent = _recording_backend()
    source = make_path(flavor, "s3://bkt/a.bin", backend=backend)
    for overwrite in (False, True):
        with pytest.raises(OSError, match="same file") as info:
            source.copy(
                make_path(flavor, "s3://bkt/a.bin", backend=backend),
                overwrite=overwrite,
            )
        assert info.value.errno == errno.EINVAL
    assert "CopyObject" not in sent and _stored(moto_s3, "a.bin") == b"keep"


def test_copy_onto_a_prefix_directory_is_refused_before_the_store_is_asked(
    flavor, moto_s3
):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"new")
    moto_s3.put_object(Bucket="bkt", Key="d/inner.txt", Body=b"inner")
    backend, sent = _recording_backend()
    source = make_path(flavor, "s3://bkt/a.bin", backend=backend)
    for overwrite in (False, True):
        with pytest.raises(IsADirectoryError):
            source.copy(
                make_path(flavor, "s3://bkt/d", backend=backend), overwrite=overwrite
            )
    assert "CopyObject" not in sent
    assert _moto_keys(moto_s3) == ["a.bin", "d/inner.txt"]


def test_copy_of_a_missing_file_or_a_prefix_without_recursive_changes_nothing(
    flavor, moto_s3
):
    moto_s3.put_object(Bucket="bkt", Key="d/inner.txt", Body=b"inner")
    backend, sent = _recording_backend()
    with pytest.raises(FileNotFoundError):
        make_path(flavor, "s3://bkt/nothing", backend=backend).copy(
            make_path(flavor, "s3://bkt/copy", backend=backend)
        )
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt/d", backend=backend).copy(
            make_path(flavor, "s3://bkt/copy", backend=backend)
        )
    assert "CopyObject" not in sent and _moto_keys(moto_s3) == ["d/inner.txt"]


def test_a_refused_copy_leaves_an_existing_target_as_it_was(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"new")
    moto_s3.put_object(Bucket="bkt", Key="b.bin", Body=b"old")
    backend, sent, attempts = _refusing_copy_backend("AccessDenied")
    with pytest.raises(PermissionError):
        make_path(flavor, "s3://bkt/a.bin", backend=backend).copy(
            make_path(flavor, "s3://bkt/b.bin", backend=backend), overwrite=True
        )
    assert attempts == ["b.bin"]
    assert "DeleteObject" not in sent
    assert _stored(moto_s3, "b.bin") == b"old"


def test_a_store_that_does_not_implement_the_copy_is_read_and_written_instead(
    flavor, moto_s3
):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    backend, sent, attempts = _refusing_copy_backend("NotImplemented")
    make_path(flavor, "s3://bkt/a.bin", backend=backend).copy(
        make_path(flavor, "s3://bkt/b.bin", backend=backend)
    )
    assert attempts == ["b.bin"]
    assert _stored(moto_s3, "b.bin") == b"payload"
    assert "GetObject" in sent and "PutObject" in sent


def test_copy_between_two_supplied_backends_streams(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    first, sent = _recording_backend()
    second, _ = _recording_backend()
    make_path(flavor, "s3://bkt/a.bin", backend=first).copy(
        make_path(flavor, "s3://bkt/b.bin", backend=second)
    )
    assert _stored(moto_s3, "b.bin") == b"payload"
    assert "CopyObject" not in sent and "GetObject" in sent


def test_recursive_copy_copies_each_file_in_the_store_and_offers_a_refused_one(
    flavor, moto_s3
):
    for key in ("d/a.txt", "d/b.txt", "d/sub/c.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=key.encode())
    backend, sent, attempts = _refusing_copy_backend("AccessDenied", "e/b.txt")
    offered = []
    make_path(flavor, "s3://bkt/d", backend=backend).copy(
        make_path(flavor, "s3://bkt/e", backend=backend),
        recursive=True,
        on_error=lambda error, path: offered.append((type(error), path.key)) or True,
    )
    assert offered == [(PermissionError, "d/b.txt")]
    assert attempts == ["e/b.txt"]
    assert "GetObject" not in sent
    assert _stored(moto_s3, "e/sub/c.txt") == b"d/sub/c.txt"
    assert "e/b.txt" not in _moto_keys(moto_s3)


def test_moving_a_prefix_copies_each_file_in_the_store(flavor, moto_s3):
    for key in ("d/a.txt", "d/sub/b.txt", "d/sub/c.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=key.encode())
    backend, sent = _recording_backend()
    make_path(flavor, "s3://bkt/d", backend=backend).move(
        make_path(flavor, "s3://bkt/f", backend=backend)
    )
    assert [key for key in _moto_keys(moto_s3) if not key.endswith("/")] == [
        "f/a.txt",
        "f/sub/b.txt",
        "f/sub/c.txt",
    ]
    assert sent.count("CopyObject") == 3
    assert "GetObject" not in sent
    assert not any(key.startswith("d/") for key in _moto_keys(moto_s3))


# --- one default backend per process ----------------------------------------


def test_separately_built_paths_share_one_default_client(flavor, moto_s3, monkeypatch):
    import boto3

    built = []
    real = boto3.client

    def counting(*args, **kwargs):
        built.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(boto3, "client", counting)
    first = make_path(flavor, "s3://bkt/a.txt")
    second = make_path(flavor, "s3://other/b.txt")
    assert first.backend is second.backend
    first.write_bytes(b"x")
    assert not second.exists()
    assert len(built) == 1


def test_each_moto_test_talks_to_its_own_mock_first(flavor, moto_s3):
    make_path(flavor, "s3://bkt/left-behind.txt").write_bytes(b"x")
    assert _moto_keys(moto_s3) == ["left-behind.txt"]


def test_each_moto_test_talks_to_its_own_mock_second(flavor, moto_s3):
    assert not make_path(flavor, "s3://bkt/left-behind.txt").exists()
    assert _moto_keys(moto_s3) == []
    make_path(flavor, "s3://bkt/mine.txt").write_bytes(b"y")
    assert _moto_keys(moto_s3) == ["mine.txt"]


def test_a_file_named_as_the_root_of_a_recursive_copy_is_copied(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    backend, _sent = _recording_backend()
    make_path(flavor, "s3://bkt/a.bin", backend=backend).copy(
        make_path(flavor, "s3://bkt/b.bin", backend=backend), recursive=True
    )
    assert _stored(moto_s3, "b.bin") == b"payload"


def test_a_streamed_read_is_one_request_whatever_its_size(flavor, moto_s3):
    # One GetObject per open: the stream is one version of the object, with no
    # ranged requests that a replacement could fall between.
    moto_s3.put_object(Bucket="bkt", Key="big.bin", Body=bytes(range(256)) * 2500)
    backend, sent = _recording_backend()
    with make_path(flavor, "s3://bkt/big.bin", backend=backend).open("rb") as stream:
        total = 0
        while piece := stream.read(32768):
            total += len(piece)
    assert total == 256 * 2500
    assert sent == ["GetObject"]


# --- a write block that raised uploads nothing ----------------------------------


class _Abort(Exception):
    pass


_WRITE_REQUESTS = {"PutObject", "CreateMultipartUpload", "UploadPart"}


@pytest.mark.parametrize("spooled", [False, True], ids=["memory", "temp-file"])
@pytest.mark.parametrize("mode, data", [("wb", b"part"), ("w", "part")])
def test_a_write_block_that_raised_sends_no_upload_moto(
    flavor, moto_s3, monkeypatch, mode, data, spooled
):
    if spooled:
        from objpath._s3 import _S3WriteStream

        monkeypatch.setattr(_S3WriteStream, "spool_size", 2)
    moto_s3.put_object(Bucket="bkt", Key="old", Body=b"OLD")
    backend, sent = _recording_backend()
    for key in ("old", "new"):
        with pytest.raises(_Abort):
            with make_path(flavor, f"s3://bkt/{key}", backend=backend).open(mode) as f:
                f.write(data)
                raise _Abort
    assert _WRITE_REQUESTS.isdisjoint(sent)
    assert _moto_keys(moto_s3) == ["old"]
    assert _stored(moto_s3, "old") == b"OLD"


@pytest.mark.parametrize("mode, data", [("r+b", b"J"), ("r+", "J")])
def test_a_read_modify_write_block_that_raised_sends_no_upload_moto(
    flavor, moto_s3, mode, data
):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"hello")
    backend, sent = _recording_backend()
    with pytest.raises(_Abort):
        with make_path(flavor, "s3://bkt/a.txt", backend=backend).open(mode) as f:
            f.write(data)
            raise _Abort
    assert _WRITE_REQUESTS.isdisjoint(sent)
    assert _stored(moto_s3, "a.txt") == b"hello"


def test_an_exclusive_create_block_that_raised_sends_no_upload_moto(flavor, moto_s3):
    backend, sent = _recording_backend()
    with pytest.raises(_Abort):
        with make_path(flavor, "s3://bkt/job.lock", backend=backend).open("xb") as f:
            f.write(b"worker1")
            raise _Abort
    assert _WRITE_REQUESTS.isdisjoint(sent)
    assert _moto_keys(moto_s3) == []


@pytest.mark.parametrize("mode, data", [("wb", b"whole"), ("w", "whole")])
def test_a_write_block_that_ended_normally_or_was_closed_uploads_the_object_moto(
    flavor, moto_s3, mode, data
):
    with make_path(flavor, "s3://bkt/a").open(mode) as f:
        f.write(data)
    f = make_path(flavor, "s3://bkt/b").open(mode)
    f.write(data)
    f.close()
    f.close()
    assert _moto_keys(moto_s3) == ["a", "b"]
    assert _stored(moto_s3, "a") == b"whole" == _stored(moto_s3, "b")


# --- the contract every Path meets, against moto --------------------------------


class TestS3Contract(PathContract):
    # Directories are key prefixes (implicit parents, a key and a prefix of
    # the same name can coexist) and objects are written whole.
    enforces_directory_hierarchy = False
    supports_append = False
    supports_question_mark_names = True
    # An object's time is when the store last wrote it.
    supports_utime = False

    @pytest.fixture
    def root(self, s3_server, flavor):
        pytest.importorskip("boto3")
        return make_path(flavor, s3_server)
