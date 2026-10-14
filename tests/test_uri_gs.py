"""The `gs:` scheme of uripath: `URIPath("gs://bucket/key")` is a `GSURIPath`
that does its I/O with the `GSPath` of the bucket and key its URL names, on the
backend it carries. What is about the path itself, whatever it is spelled as,
is in `test_gs_fake.py` and `test_gs.py`."""

from __future__ import annotations

import pickle

import pytest

uripath = pytest.importorskip("uripath")
pytest.importorskip("objpath.uri")

from pathapi.mempath import MemPath  # noqa: E402

from objpath import BaseGSBackend, GSBackend, GSPath, _gs_backend  # noqa: E402
from objpath.uri import GSURIPath  # noqa: E402
from gs_fakes import (  # noqa: E402
    _FakeBackend,
    _Forbidden,
    _bucket_with,
    _two_buckets,
)

URIPath = uripath.URIPath


class _PicklableBackend(BaseGSBackend):
    """A backend that says a path may carry it to another process."""

    picklable = True

    def __init__(self, label="one"):
        self.label = label

    def client(self):
        raise AssertionError("the test never asks this backend for a client")


# --- the class and the parts of a URL -----------------------------------------


def test_a_gs_url_is_a_gs_uri_path():
    path = URIPath("gs://bucket/key/path")
    assert type(path) is GSURIPath
    assert isinstance(path, uripath.URIPath)
    assert str(path) == "gs://bucket/key/path"


@pytest.mark.parametrize(
    "url, bucket, key",
    [
        ("gs://bkt/dir/key", "bkt", "dir/key"),
        ("gs://bkt/dir/", "bkt", "dir"),
        ("gs://bkt/dir", "bkt", "dir"),
        ("gs://bkt/", "bkt", ""),
        ("gs://bkt", "bkt", ""),
        ("gs://bkt/a//b", "bkt", "a//b"),
        ("gs://bkt/dir//", "bkt", "dir/"),
        ("gs://bkt/a%20b/%C3%BC", "bkt", "a b/ü"),
        ("gs://bkt/a/./b", "bkt", "a/b"),
    ],
)
def test_the_bucket_is_the_host_and_the_key_is_the_path_less_one_trailing_slash(
    url, bucket, key
):
    path = URIPath(url)
    assert path.bucket == bucket
    assert path.key == key


def test_a_path_joined_below_a_gs_url_keeps_the_bucket():
    path = URIPath("gs://bkt/dir") / "key"
    assert type(path) is GSURIPath
    assert (path.bucket, path.key) == ("bkt", "dir/key")
    assert (path.parent.bucket, path.parent.key) == ("bkt", "dir")


def test_a_uri_path_and_the_core_path_of_one_url_are_two_classes():
    uri_path = URIPath("gs://bkt/dir/key")
    core = GSPath("dir/key", bucket="bkt")
    assert str(uri_path) == str(core)
    assert uri_path != core
    assert (uri_path.bucket, uri_path.key) == (core.bucket, core.key)


def test_the_client_options_are_not_keywords_of_a_uri_path():
    with pytest.raises(TypeError):
        URIPath("gs://bkt/key", project="p")


# --- a URL with no bucket ------------------------------------------------------


@pytest.mark.parametrize("url", ["gs:x", "gs:///x", "gs:///"])
def test_a_url_without_a_bucket_does_not_exist_and_asks_nothing(url):
    backend, bucket = _bucket_with(**{"x": b"x"})
    path = URIPath(url, backend=backend)
    assert not path.bucket
    assert path.exists() is False
    with pytest.raises(FileNotFoundError, match="bucket"):
        path.stat()
    with pytest.raises(FileNotFoundError, match="bucket"):
        path.read_bytes()
    with pytest.raises(FileNotFoundError, match="bucket"):
        path.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="bucket"):
        list(path.iterdir())
    with pytest.raises(FileNotFoundError, match="bucket"):
        path.mkdir()
    with pytest.raises(FileNotFoundError, match="bucket"):
        path.unlink()
    assert bucket.list_calls == [] and bucket.uploads == []
    assert bucket.objects == {"x": b"x"}


# --- the backend ---------------------------------------------------------------


def test_a_supplied_backend_is_used_as_it_is_for_every_bucket():
    backend, one, two = _two_buckets()
    first = URIPath("gs://one/a.txt", backend=backend)
    second = URIPath("gs://two/dir/b.txt", backend=backend)
    assert first.backend is backend and second.backend is backend
    assert (first / "child").backend is backend
    assert first.parent.backend is backend
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    assert one.objects == {"a.txt": b"first"}
    assert two.objects == {"dir/b.txt": b"second"}
    assert second.read_bytes() == b"second"


def test_the_paths_built_without_a_backend_share_the_default_one(monkeypatch):
    first, second = URIPath("gs://one/a.txt"), URIPath("gs://two/b.txt")
    assert first.backend is second.backend
    assert first.backend is _gs_backend._default_backend()
    assert type(first.backend) is GSBackend
    assert (
        first.backend is not URIPath("gs://one/a.txt", backend=_FakeBackend()).backend
    )


def test_the_default_backend_is_the_one_a_core_path_shares():
    assert URIPath("gs://one/a.txt").backend is GSPath("b", bucket="two").backend


def test_a_path_without_a_backend_works_on_the_default_one(monkeypatch):
    backend, bucket = _bucket_with(**{"a.txt": b"content"})
    monkeypatch.setattr(_gs_backend, "_DEFAULT_BACKEND", backend)
    assert URIPath("gs://bucket/a.txt").read_bytes() == b"content"
    URIPath("gs://bucket/b.txt").write_bytes(b"new")
    assert bucket.objects["b.txt"] == b"new"


# --- the store's own copy is decided from the two URIs -------------------------


def test_a_copy_to_another_bucket_of_one_supplied_backend_is_one_copy_call():
    backend, one, two = _two_buckets()
    one.objects["a.txt"] = b"content"
    URIPath("gs://one/a.txt", backend=backend).copy(
        URIPath("gs://two/dir/b.txt", backend=backend)
    )
    assert two.objects == {"dir/b.txt": b"content"}
    assert one.copies == [("a.txt", "dir/b.txt")]
    assert one.downloads == [] and one.uploads == [] and two.uploads == []


def test_a_copy_between_paths_without_a_backend_is_one_copy_call(monkeypatch):
    backend, one, two = _two_buckets()
    monkeypatch.setattr(_gs_backend, "_DEFAULT_BACKEND", backend)
    one.objects["a.txt"] = b"content"
    URIPath("gs://one/a.txt").copy(URIPath("gs://two/b.txt"))
    assert two.objects == {"b.txt": b"content"}
    assert one.copies == [("a.txt", "b.txt")]
    assert one.downloads == []


def test_a_copy_between_two_supplied_backends_streams():
    first, one = _bucket_with(**{"a.txt": b"content"})
    second, other = _bucket_with()
    URIPath("gs://bucket/a.txt", backend=first).copy(
        URIPath("gs://bucket/b.txt", backend=second)
    )
    assert other.objects == {"b.txt": b"content"}
    assert one.copies == [] and one.downloads == ["a.txt"]
    assert other.uploads == ["b.txt"]


def test_a_copy_from_a_supplied_backend_to_the_default_one_streams(monkeypatch):
    default, other = _bucket_with()
    monkeypatch.setattr(_gs_backend, "_DEFAULT_BACKEND", default)
    supplied, one = _bucket_with(**{"a.txt": b"content"})
    URIPath("gs://bucket/a.txt", backend=supplied).copy(URIPath("gs://bucket/b.txt"))
    assert other.objects == {"b.txt": b"content"}
    assert one.copies == [] and one.downloads == ["a.txt"]


def test_a_copy_to_another_scheme_streams():
    backend, bucket = _bucket_with(**{"a.txt": b"content"})
    target = MemPath("/copy.txt")
    URIPath("gs://bucket/a.txt", backend=backend).copy(target)
    assert target.read_bytes() == b"content"
    assert bucket.copies == [] and bucket.downloads == ["a.txt"]


def test_a_recursive_copy_of_a_prefix_between_buckets_copies_each_object_in_the_store():
    backend, one, two = _two_buckets()
    one.objects.update({"d/a": b"a", "d/sub/b": b"b"})
    URIPath("gs://one/d", backend=backend).copy(
        URIPath("gs://two/e", backend=backend), recursive=True
    )
    assert {k: v for k, v in two.objects.items() if not k.endswith("/")} == {
        "e/a": b"a",
        "e/sub/b": b"b",
    }
    assert sorted(one.copies) == [("d/a", "e/a"), ("d/sub/b", "e/sub/b")]
    assert one.downloads == []


# --- rename ---------------------------------------------------------------------


def test_a_sibling_rename_returns_the_new_uri_path():
    backend, bucket = _bucket_with(**{"dir/a.txt": b"content"})
    moved = URIPath("gs://bucket/dir/a.txt", backend=backend).rename("b.txt")
    assert type(moved) is GSURIPath
    assert str(moved) == "gs://bucket/dir/b.txt"
    assert moved.backend is backend
    assert bucket.objects == {"dir/b.txt": b"content"}


def test_a_rename_onto_a_uri_of_the_same_bucket_returns_that_path():
    backend, bucket = _bucket_with(**{"a.txt": b"content"})
    target = URIPath("gs://bucket/dir/b.txt", backend=backend)
    assert URIPath("gs://bucket/a.txt", backend=backend).rename(target) == target
    assert bucket.objects == {"dir/b.txt": b"content"}


def test_a_rename_across_buckets_is_not_implemented_so_move_copies():
    backend, one, two = _two_buckets()
    one.objects["a.txt"] = b"content"
    source = URIPath("gs://one/a.txt", backend=backend)
    target = URIPath("gs://two/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        source.rename(target)
    assert one.objects == {"a.txt": b"content"} and two.objects == {}
    source.move(target)
    assert one.objects == {} and two.objects == {"a.txt": b"content"}
    assert one.copies == [("a.txt", "a.txt")] and one.downloads == []


# --- pickling ---------------------------------------------------------------------


def test_a_pickled_uri_path_carries_its_url():
    path = URIPath("gs://bkt/dir//key")
    copy = pickle.loads(pickle.dumps(path))
    assert type(copy) is GSURIPath
    assert copy == path
    assert (copy.bucket, copy.key) == ("bkt", "dir//key")


def test_a_pickled_uri_path_does_not_carry_a_supplied_backend():
    path = URIPath("gs://bkt/key", backend=_FakeBackend())
    copy = pickle.loads(pickle.dumps(path))
    assert copy == path
    assert copy.backend is _gs_backend._default_backend()


def test_a_pickled_uri_path_carries_a_backend_that_says_it_is_picklable():
    path = URIPath("gs://bkt/key", backend=_PicklableBackend("mine"))
    copy = pickle.loads(pickle.dumps(path))
    assert copy == path
    assert type(copy.backend) is _PicklableBackend
    assert copy.backend.label == "mine"


# --- the callbacks get URI paths ------------------------------------------------------


def test_rm_offers_a_failed_key_to_ignore_error_as_a_uri_path():
    backend, bucket = _bucket_with(**{"dir/a": b"a", "dir/b": b"b", "dir/c": b"c"})
    bucket.delete_raises["dir/b"] = _Forbidden("403 object under hold")
    offered = []
    root = URIPath("gs://bucket/dir", backend=backend)
    root.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append((type(error), where)) or True,
    )
    assert [(kind, type(where), str(where)) for kind, where in offered] == [
        (PermissionError, GSURIPath, "gs://bucket/dir/b")
    ]
    assert offered[0][1].backend is backend
    assert set(bucket.objects) == {"dir/b"}


def test_rm_offers_the_path_itself_when_nothing_is_there():
    backend, _bucket = _bucket_with()
    path = URIPath("gs://bucket/missing", backend=backend)
    offered = []
    path.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append((type(error), where)) or True,
    )
    assert len(offered) == 1
    assert offered[0][0] is FileNotFoundError
    assert type(offered[0][1]) is GSURIPath
    assert offered[0][1] == path


def test_copy_offers_a_failed_object_to_on_error_as_a_uri_path():
    source_backend, bucket = _bucket_with(**{"d/a": b"a", "d/b": b"b"})

    def refuse(name):
        if name == "d/b":
            raise _Forbidden("403")

    bucket.before_read = refuse
    target_backend, target = _bucket_with()
    offered = []
    URIPath("gs://bucket/d", backend=source_backend).copy(
        URIPath("gs://bucket/e", backend=target_backend),
        recursive=True,
        on_error=lambda error, where: offered.append((type(error), where)) or True,
    )
    assert [(kind, type(where), str(where)) for kind, where in offered] == [
        (PermissionError, GSURIPath, "gs://bucket/d/b")
    ]
    assert target.objects["e/a"] == b"a"
    assert "e/b" not in target.objects


def test_copy_reports_its_progress_with_the_uri_path_that_was_copied():
    backend, one, two = _two_buckets()
    one.objects["a.txt"] = b"content"
    source = URIPath("gs://one/a.txt", backend=backend)
    calls = []
    source.copy(
        URIPath("gs://two/b.txt", backend=backend),
        progress=lambda path, copied, total: calls.append((path, copied, total)),
    )
    assert calls == [(source, 7, 7)]
    assert calls[0][0] is source
