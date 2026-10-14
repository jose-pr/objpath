"""`GSPath`: what its constructor accepts, which paths are equal, how it is
spelled, which backend it holds and what a pickled one carries. Nothing here
reaches the Google SDK or a server."""

from __future__ import annotations

import pickle
import sys
import threading

import pytest

from objpath import BaseGSBackend, GSBackend, GSPath, S3Path, _gs_backend


class _Backend(BaseGSBackend):
    """A backend of a test's own: it is a valid one, and `GSPath` never asks
    it for a client unless the test does."""

    def client(self):
        raise AssertionError("the test never asks this backend for a client")


class _PicklableBackend(BaseGSBackend):
    """A backend that says a path may carry it to another process."""

    picklable = True

    def __init__(self, label="one"):
        self.label = label

    def client(self):
        raise AssertionError("the test never asks this backend for a client")


@pytest.fixture
def built_clients(monkeypatch):
    """Every client a `GSBackend` builds, in order; the SDK is not asked."""
    built = []

    def build(self):
        client = object()
        built.append(client)
        return client

    monkeypatch.setattr(GSBackend, "_build_client", build)
    return built


# --- the bucket ---------------------------------------------------------------


def test_a_path_needs_a_bucket():
    with pytest.raises(TypeError, match="bucket"):
        GSPath("dir/key")
    with pytest.raises(TypeError, match="bucket"):
        GSPath()
    with pytest.raises(TypeError, match="bucket"):
        GSPath("dir", "key", backend=_Backend())


@pytest.mark.parametrize("bucket", ["", "a/b", "/"])
def test_a_bucket_is_a_name_with_no_slash(bucket):
    with pytest.raises(ValueError, match="bucket"):
        GSPath("key", bucket=bucket)


def test_a_bucket_is_a_string():
    with pytest.raises(TypeError, match="bucket"):
        GSPath("key", bucket=7)


def test_the_bucket_and_the_key_are_what_the_path_names():
    path = GSPath("dir/key", bucket="bkt")
    assert path.bucket == "bkt"
    assert path.key == "dir/key"
    root = GSPath(bucket="bkt")
    assert root.bucket == "bkt" and root.key == ""


def test_a_path_joined_from_a_gs_path_takes_its_bucket():
    base = GSPath("dir", bucket="bkt")
    joined = GSPath(base, "key")
    assert joined.bucket == "bkt"
    assert joined.key == "dir/key"
    assert (base / "key").bucket == "bkt"
    assert base.joinpath("a", "b").bucket == "bkt"
    assert base.parent.bucket == "bkt"
    assert base.with_name("other").bucket == "bkt"
    assert GSPath(base, "/abs").key == "abs"
    # Any segment that is a path supplies the bucket.
    later = GSPath("/abs", base)
    assert (later.bucket, later.key) == ("bkt", "abs/dir")


def test_a_bucket_given_to_a_joined_path_moves_it_and_keeps_the_backend():
    backend = _Backend()
    base = GSPath("dir/key", bucket="one", backend=backend)
    moved = GSPath(base, bucket="two")
    assert moved.bucket == "two"
    assert moved.key == "dir/key"
    assert moved.backend is backend
    assert base.bucket == "one"
    assert GSPath(base, "more", bucket="two").key == "dir/key/more"


def test_a_joined_path_keeps_its_backend():
    backend = _Backend()
    base = GSPath("dir", bucket="bkt", backend=backend)
    assert GSPath(base, "key").backend is backend
    assert (base / "key").backend is backend
    assert base.joinpath("a").backend is backend
    assert base.parent.backend is backend
    assert base.with_name("n").backend is backend
    assert base.with_segments("x", "y").backend is backend
    own = GSPath("dir", bucket="bkt", project="p")
    assert (own / "key").backend is own.backend


def test_a_joined_path_with_client_options_gets_a_backend_of_its_own():
    base = GSPath("dir", bucket="bkt", project="one")
    joined = GSPath(base, "key", project="two")
    assert joined.backend is not base.backend
    assert joined.backend.client_kwargs == {"project": "two"}
    assert base.backend.client_kwargs == {"project": "one"}


# --- the backend --------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        {"project": "p"},
        {"credentials": object()},
        {"client_options": {"api_endpoint": "http://127.0.0.1:1"}},
        {"timeout": 5},
        {"retry": None},
        {"timeout": 5, "retry": None, "project": "p"},
    ],
    ids=["project", "credentials", "client_options", "timeout", "retry-none", "all"],
)
def test_a_backend_together_with_a_client_option_is_a_type_error(options):
    with pytest.raises(TypeError, match="backend"):
        GSPath("key", bucket="bkt", backend=_Backend(), **options)


def test_client_options_build_the_backend_of_the_path():
    credentials = object()
    path = GSPath(
        "key",
        bucket="bkt",
        project="p",
        credentials=credentials,
        client_options={"api_endpoint": "http://127.0.0.1:1"},
        timeout=(3, 9),
        retry=None,
    )
    backend = path.backend
    assert type(backend) is GSBackend
    assert backend.client_kwargs == {
        "project": "p",
        "credentials": credentials,
        "client_options": {"api_endpoint": "http://127.0.0.1:1"},
    }
    assert backend.call_options() == {"timeout": (3, 9), "retry": None}
    assert backend is not _gs_backend._default_backend()


def test_timeout_and_retry_alone_build_the_backend_of_the_path():
    path = GSPath("key", bucket="bkt", timeout=5)
    assert type(path.backend) is GSBackend
    assert path.backend.call_options() == {"timeout": 5}
    assert path.backend.client_kwargs == {}
    assert path.backend is not _gs_backend._default_backend()
    assert GSPath("key", bucket="bkt", retry=None).backend.call_options() == {
        "retry": None
    }


def test_two_paths_with_client_options_do_not_share_a_backend(built_clients):
    first = GSPath("a", bucket="bkt", project="p")
    second = GSPath("b", bucket="bkt", project="p")
    assert first.backend is not second.backend
    first.backend.client()
    second.backend.client()
    assert len(built_clients) == 2


def test_a_supplied_backend_is_the_backend_of_the_path():
    backend = _Backend()
    path = GSPath("a", bucket="bkt", backend=backend)
    assert path.backend is backend
    assert GSPath("b", bucket="other", backend=backend).backend is backend
    assert path.backend is not GSPath("c", bucket="bkt").backend


def test_separately_built_paths_share_one_default_backend_and_client(built_clients):
    first, second = GSPath("a.txt", bucket="one"), GSPath("b.txt", bucket="two")
    assert first.backend is second.backend
    assert first.backend is _gs_backend._default_backend()
    assert type(first.backend) is GSBackend
    assert first.backend.call_options() == {}
    first.backend.client()
    second.backend.client()
    assert len(built_clients) == 1


def test_threads_racing_for_the_default_backend_get_one():
    found = []
    barrier = threading.Barrier(8)

    def build():
        barrier.wait()
        found.append(GSPath("x", bucket="one").backend)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(found) == 8 and len({id(backend) for backend in found}) == 1


def test_a_test_may_build_the_default_backend():
    assert GSPath("a.txt", bucket="one").backend is _gs_backend._DEFAULT_BACKEND


def test_the_next_test_starts_without_the_default_backend_of_the_one_before():
    # Follows the test above: the suite forgets the shared backend between tests.
    assert _gs_backend._DEFAULT_BACKEND is None


def test_a_path_made_before_the_default_backend_was_forgotten_keeps_it():
    path = GSPath("a", bucket="one")
    before = path.backend
    _gs_backend._reset_default_backend()
    assert path.backend is before
    assert GSPath("b", bucket="one").backend is not before


# --- identity ------------------------------------------------------------------


def test_paths_of_one_bucket_and_key_are_equal_and_hash_alike():
    first = GSPath("dir/key", bucket="bkt")
    second = GSPath("dir", "key", bucket="bkt")
    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second}) == 1


def test_a_path_of_another_bucket_is_a_different_path():
    one = GSPath("key", bucket="one")
    two = GSPath("key", bucket="two")
    assert one != two
    assert hash(one) != hash(two)
    assert len({one, two}) == 2
    assert {one: 1}.get(two) is None


def test_a_path_of_another_key_is_a_different_path():
    assert GSPath("a", bucket="bkt") != GSPath("b", bucket="bkt")
    assert GSPath("a/b", bucket="bkt") != GSPath("a", bucket="bkt")


def test_the_backend_is_not_part_of_the_identity():
    first = GSPath("key", bucket="bkt", backend=_Backend())
    second = GSPath("key", bucket="bkt", backend=_Backend())
    assert first == second
    assert hash(first) == hash(second)


def test_x_and_slash_x_are_the_same_key():
    relative, rooted = GSPath("x", bucket="bkt"), GSPath("/x", bucket="bkt")
    assert relative == rooted
    assert hash(relative) == hash(rooted)
    assert relative.key == rooted.key == "x"
    assert not relative.is_absolute()
    assert rooted.is_absolute()
    assert GSPath("a/b", bucket="bkt") == GSPath("/a/b", bucket="bkt")


def test_a_path_is_equal_to_no_other_kind_of_object():
    path = GSPath("key", bucket="bkt")
    assert path != "gs://bkt/key"
    assert path != S3Path("key", bucket="bkt")
    assert path != ("bkt", "key")


def test_paths_sort_by_bucket_then_key():
    paths = [
        GSPath("b", bucket="two"),
        GSPath("z", bucket="one"),
        GSPath("a", bucket="two"),
    ]
    assert [(path.bucket, path.key) for path in sorted(paths)] == [
        ("one", "z"),
        ("two", "a"),
        ("two", "b"),
    ]


# --- the key -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, key",
    [
        ("dir", "dir"),
        ("dir/", "dir"),
        ("dir//", "dir/"),
        ("dir///", "dir//"),
        ("", ""),
        ("/", ""),
        ("/x", "x"),
        ("//x", "x"),
        ("a//b", "a//b"),
        ("a///b", "a///b"),
        ("a/./b", "a/b"),
        ("./a", "a"),
        ("a/../b", "a/../b"),
        ("..", ".."),
    ],
)
def test_the_key_drops_one_trailing_slash_and_keeps_an_interior_empty_name(name, key):
    assert GSPath(name, bucket="bkt").key == key


def test_a_trailing_slash_does_not_make_another_path():
    assert GSPath("dir/", bucket="bkt") == GSPath("dir", bucket="bkt")
    assert GSPath("dir//", bucket="bkt") != GSPath("dir", bucket="bkt")
    assert GSPath("dir//", bucket="bkt").key == "dir/"
    assert GSPath("a//b", bucket="bkt") != GSPath("a/b", bucket="bkt")


def test_the_slash_that_ends_a_piece_separates_it_from_the_next():
    assert GSPath("a/", "b", bucket="bkt").key == "a/b"
    assert GSPath("a", "/b", bucket="bkt").key == "b"
    assert GSPath("a", "", "b", bucket="bkt").key == "a/b"


def test_a_segment_that_is_not_text_is_a_type_error():
    with pytest.raises(TypeError):
        GSPath(7, bucket="bkt")
    with pytest.raises(TypeError):
        GSPath("a", None, bucket="bkt")


# --- spelling ------------------------------------------------------------------


def test_str_is_the_url():
    assert str(GSPath("dir/key", bucket="bkt")) == "gs://bkt/dir/key"
    assert str(GSPath("/dir/key", bucket="bkt")) == "gs://bkt/dir/key"
    assert str(GSPath("dir/", bucket="bkt")) == "gs://bkt/dir"
    assert str(GSPath(bucket="bkt")) == "gs://bkt/"
    assert GSPath("dir/key", bucket="bkt").as_uri() == "gs://bkt/dir/key"


def test_the_url_spells_a_key_that_ends_in_a_slash_with_one_more():
    assert str(GSPath("dir//", bucket="bkt")) == "gs://bkt/dir//"
    assert str(GSPath("a//b", bucket="bkt")) == "gs://bkt/a//b"


def test_the_url_escapes_what_a_url_cannot_hold():
    path = GSPath("a b/ü?#%", bucket="bkt")
    assert str(path) == "gs://bkt/a%20b/%C3%BC%3F%23%25"
    assert path.key == "a b/ü?#%"


def test_repr_names_the_key_and_the_bucket():
    assert repr(GSPath("dir/key", bucket="bkt")) == "GSPath('dir/key', bucket='bkt')"
    assert repr(GSPath(bucket="bkt")) == "GSPath('', bucket='bkt')"
    assert repr(GSPath("/dir", bucket="bkt")) == "GSPath('/dir', bucket='bkt')"


def test_repr_of_a_path_with_its_own_backend_names_the_options_and_no_value():
    path = GSPath(
        "key", bucket="bkt", project="secret-project", credentials="secret-token"
    )
    shown = repr(path)
    assert shown.startswith("GSPath('key', bucket='bkt', backend=GSBackend(")
    assert "project" in shown and "credentials" in shown
    assert "secret-project" not in shown and "secret-token" not in shown


def test_nothing_that_is_pure_needs_the_sdk(monkeypatch):
    # `None` in `sys.modules` makes the import of the SDK fail as if it were
    # not installed.
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    path = GSPath("dir/key", bucket="bkt", project="p", timeout=3)
    child = path / "more"
    assert child.parent == path
    assert (child.bucket, child.key, child.name) == ("bkt", "dir/key/more", "more")
    assert str(child) == "gs://bkt/dir/key/more"
    assert repr(child).startswith("GSPath(")
    assert hash(child) == hash(GSPath("dir/key/more", bucket="bkt"))
    assert pickle.loads(pickle.dumps(child)) == child


# --- renaming and the class ------------------------------------------------------


def test_rename_onto_another_class_is_not_implemented_and_asks_nothing():
    path = GSPath("a", bucket="bkt", backend=_Backend())
    with pytest.raises(NotImplementedError):
        path.rename(S3Path("a", bucket="bkt"))


def test_rename_onto_another_bucket_or_backend_is_not_implemented_and_asks_nothing():
    backend = _Backend()
    path = GSPath("a", bucket="one", backend=backend)
    with pytest.raises(NotImplementedError):
        path.rename(GSPath("a", bucket="two", backend=backend))
    with pytest.raises(NotImplementedError):
        path.rename(GSPath("b", bucket="one", backend=_Backend()))


# --- pickling ----------------------------------------------------------------------


def test_a_pickled_path_carries_its_bucket_and_names():
    path = GSPath("dir/key", bucket="bkt")
    copy = pickle.loads(pickle.dumps(path))
    assert type(copy) is GSPath
    assert copy == path
    assert (copy.bucket, copy.key) == ("bkt", "dir/key")
    rooted = pickle.loads(pickle.dumps(GSPath("/dir//", bucket="bkt")))
    assert rooted == GSPath("/dir//", bucket="bkt")
    assert rooted.is_absolute()
    assert rooted.key == "dir/"


def test_a_pickled_path_carries_no_client_option():
    credentials = "secret-token-in-the-credentials"
    path = GSPath(
        "key",
        bucket="bkt",
        project="secret-project",
        credentials=credentials,
        timeout=4,
    )
    data = pickle.dumps(path)
    assert b"secret-project" not in data
    assert b"secret-token-in-the-credentials" not in data
    copy = pickle.loads(data)
    assert copy == path
    # The receiving process builds the default backend.
    assert copy.backend is _gs_backend._default_backend()
    assert copy.backend is not path.backend


def test_a_pickled_path_does_not_carry_a_supplied_backend_that_is_not_picklable():
    path = GSPath("key", bucket="bkt", backend=_Backend())
    assert _Backend.picklable is False
    copy = pickle.loads(pickle.dumps(path))
    assert copy.backend is _gs_backend._default_backend()


def test_a_pickled_path_carries_a_backend_that_says_it_is_picklable():
    path = GSPath("key", bucket="bkt", backend=_PicklableBackend("mine"))
    copy = pickle.loads(pickle.dumps(path))
    assert copy == path
    assert type(copy.backend) is _PicklableBackend
    assert copy.backend.label == "mine"


def test_a_pickled_backend_keeps_its_options_and_builds_its_own_client(built_clients):
    backend = GSBackend(project="p", timeout=7)
    first = backend.client()
    copy = pickle.loads(pickle.dumps(backend))
    assert copy.client_kwargs == {"project": "p"}
    assert copy.call_options() == {"timeout": 7}
    assert copy.client() is not first
    assert len(built_clients) == 2
