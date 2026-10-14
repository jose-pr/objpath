"""The constructor and the identity rules of `S3Path`: where the bucket comes
from, which backend a path has, what makes two paths equal, how a key is
spelled, how a path is shown, and what a pickle carries. None of these tests
sends a request."""

from __future__ import annotations

import pickle
import threading

import pytest

from objpath import BaseS3Backend, S3Backend, S3Path
from objpath import _s3_backend


class _PicklableBackend(BaseS3Backend):
    """A backend that says a pickled path may carry it."""

    picklable = True

    def __init__(self, tag):
        self.tag = tag

    def client(self):
        raise AssertionError("a path that is only built asks for no client")


class _OtherBackend(BaseS3Backend):
    def client(self):
        raise AssertionError("a path that is only built asks for no client")


def test_s3path_is_a_pathlib_next_path_that_needs_no_uri():
    from pathlib_next import Path

    assert isinstance(S3Path("dir/key", bucket="bkt"), Path)


# --- the bucket ------------------------------------------------------------------


def test_a_path_without_a_bucket_is_a_type_error():
    with pytest.raises(TypeError, match="bucket"):
        S3Path("key")
    with pytest.raises(TypeError, match="bucket"):
        S3Path()


def test_a_path_joined_from_other_segments_that_name_no_bucket_is_a_type_error():
    with pytest.raises(TypeError, match="bucket"):
        S3Path("a", "b")


@pytest.mark.parametrize(
    "bucket, error",
    [("", ValueError), ("a/b", ValueError), ("/bkt", ValueError), (3, TypeError)],
)
def test_the_bucket_must_be_a_name_with_no_slash(bucket, error):
    with pytest.raises(error, match="bucket"):
        S3Path("key", bucket=bucket)


def test_the_bucket_is_taken_from_a_segment_that_is_an_s3path():
    base = S3Path("dir", bucket="bkt")
    joined = S3Path(base, "key")
    assert joined.bucket == "bkt"
    assert joined.key == "dir/key"
    assert S3Path("a", base).bucket == "bkt"


def test_the_bucket_keyword_moves_a_joined_path_to_another_bucket():
    own = _OtherBackend()
    base = S3Path("dir/key", bucket="bkt", backend=own)
    moved = S3Path(base, bucket="other")
    assert (moved.bucket, moved.key) == ("other", "dir/key")
    assert moved.backend is own
    assert base.bucket == "bkt"


def test_a_bucket_is_part_of_the_path_not_of_its_key():
    path = S3Path("dir/key", bucket="bkt")
    assert path.bucket == "bkt"
    assert path.key == "dir/key"
    assert S3Path("", bucket="bkt").key == ""


# --- the backend -----------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        {"region_name": "eu-west-1"},
        {"endpoint_url": "http://127.0.0.1:9", "aws_access_key_id": "key"},
        {"config": object()},
    ],
    ids=["region", "endpoint-and-key", "config"],
)
def test_backend_together_with_client_options_is_a_type_error(options):
    with pytest.raises(TypeError, match="backend"):
        S3Path("key", bucket="bkt", backend=S3Backend(), **options)
    with pytest.raises(TypeError, match="backend"):
        S3Path(S3Path("a", bucket="bkt"), "key", backend=S3Backend(), **options)


def test_client_options_build_the_paths_own_backend():
    path = S3Path("key", bucket="bkt", region_name="eu-west-1", endpoint_url="http://h")
    assert isinstance(path.backend, S3Backend)
    assert path.backend.client_kwargs == {
        "region_name": "eu-west-1",
        "endpoint_url": "http://h",
    }
    assert path.backend is not _s3_backend._default_backend()
    again = S3Path(
        "key", bucket="bkt", region_name="eu-west-1", endpoint_url="http://h"
    )
    assert again.backend is not path.backend


# boto3 announces the end of its Python 3.9 support when it creates its first
# client.
@pytest.mark.filterwarnings(r"ignore:.*Python 3\.9.*:Warning:boto3(\..*)?$")
def test_the_options_of_a_path_reach_its_client():
    pytest.importorskip("boto3")
    path = S3Path(
        "key", bucket="bkt", region_name="eu-west-1", endpoint_url="http://127.0.0.1:9"
    )
    with path.backend:
        client = path.backend.client()
        assert client.meta.region_name == "eu-west-1"
        assert client.meta.endpoint_url == "http://127.0.0.1:9"


def test_a_path_built_with_options_is_not_on_the_default_backend():
    shared = S3Path("a", bucket="bkt")
    own = S3Path("a", bucket="bkt", region_name="eu-west-1")
    assert shared.backend is _s3_backend._default_backend()
    assert own.backend is not shared.backend


def test_options_on_a_joined_path_build_a_new_backend():
    base = S3Path("a", bucket="bkt", region_name="eu-west-1")
    joined = S3Path(base, "b", region_name="us-east-2")
    assert joined.backend is not base.backend
    assert joined.backend.client_kwargs == {"region_name": "us-east-2"}
    assert joined.bucket == "bkt"


def test_a_supplied_backend_is_used_as_it_is_for_every_bucket():
    own = _OtherBackend()
    first = S3Path("a", bucket="one", backend=own)
    second = S3Path("b", bucket="two", backend=own)
    assert first.backend is own and second.backend is own
    assert first != second


def test_a_supplied_backend_replaces_the_one_of_a_joined_path():
    base = S3Path("a", bucket="bkt", backend=_OtherBackend())
    replacement = _OtherBackend()
    assert S3Path(base, "b", backend=replacement).backend is replacement


def test_a_joined_path_keeps_the_backend_of_the_path_it_comes_from():
    own = _OtherBackend()
    base = S3Path("dir", bucket="bkt", backend=own)
    derived = [
        S3Path(base, "key"),
        base / "key",
        base.joinpath("a", "b"),
        base.parent,
        base.with_name("other"),
        base / "key" / "..",
    ]
    for path in derived:
        assert path.backend is own
        assert path.bucket == "bkt"


def test_children_of_a_listing_keep_the_bucket_and_the_backend():
    class Client:
        def get_paginator(self, name):
            class Pages:
                def paginate(self, **kwargs):
                    yield {"Contents": [{"Key": "dir/a"}, {"Key": "dir/b"}]}

            return Pages()

    class Backend(BaseS3Backend):
        def client(self):
            return Client()

    own = Backend()
    children = list(S3Path("dir", bucket="bkt", backend=own).iterdir())
    assert [child.key for child in children] == ["dir/a", "dir/b"]
    assert all(child.bucket == "bkt" and child.backend is own for child in children)


def test_building_and_joining_paths_creates_no_client(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def forbidden(*args, **kwargs):
        raise AssertionError("a path that is only built must not create a client")

    monkeypatch.setattr(boto3, "client", forbidden)
    path = S3Path("dir/key", bucket="bkt", region_name="eu-west-1")
    joined = path / "other" / ".." / "x"
    assert joined.parent.name == ".."
    repr(joined), str(joined), hash(joined), pickle.dumps(joined)
    assert path.backend._client is None


# --- one default backend per process --------------------------------------------


def test_separately_built_paths_share_one_default_backend():
    first = S3Path("a.txt", bucket="one")
    second = S3Path("b.txt", bucket="two")
    assert first.backend is second.backend
    assert first.backend is _s3_backend._default_backend()
    assert first.backend._client is None


def test_a_supplied_backend_is_not_the_shared_one():
    own = S3Backend()
    path = S3Path("a.txt", bucket="bkt", backend=own)
    assert path.backend is own
    assert path.backend is not S3Path("b.txt", bucket="bkt").backend
    assert (path / "child").backend is own


def test_a_path_keeps_the_default_backend_it_was_built_with():
    before = S3Path("a", bucket="bkt")
    _s3_backend._reset_default_backend()
    after = S3Path("a", bucket="bkt")
    assert before.backend is not after.backend
    assert (before / "b").backend is before.backend
    assert S3Path("c", bucket="bkt").backend is after.backend


def test_threads_racing_for_the_default_backend_get_one():
    found = []
    barrier = threading.Barrier(8)

    def build():
        barrier.wait()
        found.append(S3Path("x", bucket="bkt").backend)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(found) == 8 and len({id(backend) for backend in found}) == 1


def test_a_test_may_build_the_default_backend():
    assert S3Path("a.txt", bucket="bkt").backend is _s3_backend._DEFAULT_BACKEND


def test_the_next_test_starts_without_the_default_backend_of_the_one_before():
    # Follows the test above: the suite forgets the shared backend between tests.
    assert _s3_backend._DEFAULT_BACKEND is None


# --- equality and hash ----------------------------------------------------------


def test_a_key_with_and_without_a_leading_slash_is_one_path():
    plain, rooted = S3Path("x", bucket="bkt"), S3Path("/x", bucket="bkt")
    assert plain == rooted
    assert hash(plain) == hash(rooted)
    assert len({plain, rooted}) == 1
    assert plain.key == rooted.key == "x"
    assert not plain.is_absolute()
    assert rooted.is_absolute()


def test_the_same_key_in_two_buckets_is_two_paths():
    one, two = S3Path("x", bucket="one"), S3Path("x", bucket="two")
    assert one != two
    assert not one == two
    assert len({one, two}) == 2
    assert hash(one) != hash(two)
    assert one.key == two.key


def test_the_same_bucket_and_key_are_equal_whatever_the_backend():
    first = S3Path("a/b", bucket="bkt", backend=_OtherBackend())
    second = S3Path("a/b", bucket="bkt", region_name="eu-west-1")
    assert first == second
    assert hash(first) == hash(second)


def test_different_keys_are_different_paths():
    assert S3Path("a", bucket="bkt") != S3Path("b", bucket="bkt")
    assert S3Path("dir", bucket="bkt") != S3Path("dir//", bucket="bkt")


def test_a_path_equals_only_a_path_of_its_own_class():
    class Sibling(S3Path):
        __slots__ = ()

    path = S3Path("a", bucket="bkt")
    assert path != "a"
    assert path != "s3://bkt/a"
    assert path != Sibling("a", bucket="bkt")
    assert not path == Sibling("a", bucket="bkt")


def test_equal_paths_built_by_joining_are_equal():
    base = S3Path("dir", bucket="bkt")
    assert base / "key" == S3Path("dir/key", bucket="bkt")
    assert S3Path(base, "key") == S3Path("/dir/key", bucket="bkt")


# --- the key --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, key",
    [
        ("dir/", "dir"),
        ("dir", "dir"),
        ("a/b/", "a/b"),
        ("", ""),
        ("/", ""),
        ("/x", "x"),
        # Exactly one trailing slash goes.
        ("dir//", "dir/"),
        ("dir///", "dir//"),
        # An interior empty segment is part of the key; leading slashes are not.
        ("a//b", "a//b"),
        ("//a", "a"),
        # "." segments are dropped; ".." is a name.
        ("./a/./b", "a/b"),
        ("a/../b", "a/../b"),
    ],
)
def test_key_rules(text, key):
    assert S3Path(text, bucket="bkt").key == key


def test_a_trailing_slash_names_the_same_path():
    assert S3Path("dir/", bucket="bkt") == S3Path("dir", bucket="bkt")
    assert hash(S3Path("dir/", bucket="bkt")) == hash(S3Path("dir", bucket="bkt"))


def test_a_piece_that_ends_in_a_slash_is_separated_from_the_next():
    assert S3Path("a/", "b", bucket="bkt").key == "a/b"
    assert S3Path("a", "b/", bucket="bkt").key == "a/b"


def test_an_absolute_segment_replaces_what_came_before():
    assert S3Path("a", "/b", bucket="bkt").key == "b"


def test_the_parts_of_a_key_are_its_names():
    path = S3Path("dir/sub/key.txt", bucket="bkt")
    assert path.name == "key.txt"
    assert path.suffix == ".txt"
    assert path.parent.key == "dir/sub"
    assert path.parent.parent.parent.key == ""


@pytest.mark.parametrize(
    "text, segments",
    [
        ("", ()),
        ("/", ("", "")),
        ("a/b", ("a", "b")),
        ("/a/b", ("", "a", "b")),
        ("///a", ("", "a")),
        ("a//b", ("a", "", "b")),
        ("a/./b", ("a", "b")),
        ("dir//", ("dir", "")),
    ],
)
def test_the_segments_of_a_path_are_its_names_rooted_or_not(text, segments):
    assert S3Path(text, bucket="bkt").segments == segments


# --- repr and str ---------------------------------------------------------------


def test_repr_names_the_key_and_the_bucket():
    assert repr(S3Path("dir/key", bucket="bkt")) == "S3Path('dir/key', bucket='bkt')"
    assert repr(S3Path("/dir/key", bucket="bkt")) == "S3Path('/dir/key', bucket='bkt')"
    assert repr(S3Path("", bucket="bkt")) == "S3Path('', bucket='bkt')"


def test_repr_of_a_path_with_its_own_backend_shows_the_backend_but_no_option_value():
    path = S3Path(
        "key",
        bucket="bkt",
        region_name="eu-west-1",
        aws_secret_access_key="TOPSECRETVALUE",
    )
    shown = repr(path)
    assert shown.startswith("S3Path('key', bucket='bkt', backend=S3Backend(")
    assert "region_name" in shown and "aws_secret_access_key" in shown
    assert "TOPSECRETVALUE" not in shown and "eu-west-1" not in shown


def test_str_is_the_url():
    assert str(S3Path("dir/key", bucket="bkt")) == "s3://bkt/dir/key"
    assert str(S3Path("/dir/key", bucket="bkt")) == "s3://bkt/dir/key"
    assert str(S3Path("", bucket="bkt")) == "s3://bkt/"
    assert str(S3Path("/", bucket="bkt")) == "s3://bkt/"
    assert S3Path("dir/key", bucket="bkt").as_uri() == "s3://bkt/dir/key"


def test_the_url_escapes_what_a_url_cannot_hold():
    path = S3Path("a b#c?d%e", bucket="bkt")
    assert str(path) == "s3://bkt/a%20b%23c%3Fd%25e"
    assert path.key == "a b#c?d%e"


def test_the_url_of_a_key_that_ends_in_a_slash_names_that_key():
    assert str(S3Path("dir//", bucket="bkt")) == "s3://bkt/dir//"
    assert str(S3Path("a//b", bucket="bkt")) == "s3://bkt/a//b"


# --- pickling -------------------------------------------------------------------


@pytest.mark.parametrize("protocol", range(2, pickle.HIGHEST_PROTOCOL + 1))
def test_a_pickled_path_keeps_its_bucket_and_names(protocol):
    path = S3Path("dir/key", bucket="bkt")
    restored = pickle.loads(pickle.dumps(path, protocol))
    assert restored == path
    assert (restored.bucket, restored.key) == ("bkt", "dir/key")
    assert restored.is_absolute() is False
    rooted = pickle.loads(pickle.dumps(S3Path("/dir/key", bucket="bkt"), protocol))
    assert rooted.is_absolute() is True
    assert rooted == path


def test_a_pickled_path_on_the_default_backend_gets_the_default_backend():
    restored = pickle.loads(pickle.dumps(S3Path("a", bucket="bkt")))
    assert restored.backend is _s3_backend._default_backend()


def test_a_pickled_path_carries_no_client_option():
    path = S3Path(
        "dir/key",
        bucket="bkt",
        region_name="eu-west-1",
        endpoint_url="http://internal.example",
        aws_access_key_id="AKIAEXAMPLEKEYID",
        aws_secret_access_key="TOPSECRETVALUE",
    )
    data = pickle.dumps(path)
    for text in (
        b"TOPSECRETVALUE",
        b"AKIAEXAMPLEKEYID",
        b"internal.example",
        b"eu-west",
    ):
        assert text not in data
    restored = pickle.loads(data)
    assert restored == path
    assert restored.backend is _s3_backend._default_backend()
    assert restored.backend.client_kwargs == {}


def test_a_pickled_path_carries_a_backend_that_says_it_can_travel():
    path = S3Path("a/b", bucket="bkt", backend=_PicklableBackend("tag"))
    restored = pickle.loads(pickle.dumps(path))
    assert restored == path
    assert isinstance(restored.backend, _PicklableBackend)
    assert restored.backend.tag == "tag"


def test_a_pickled_path_leaves_a_backend_that_does_not_say_so():
    path = S3Path("a/b", bucket="bkt", backend=_OtherBackend())
    restored = pickle.loads(pickle.dumps(path))
    assert restored == path
    assert not isinstance(restored.backend, _OtherBackend)
