"""`AZURIPath`, the `az:` scheme of uripath: a URL names the account (its
authority), the container (its first segment) and the blob (the rest), the
account is a directory whose children are its containers, and a backend the
caller supplies is used as it is for every path. What a path of the scheme does
once it has a container is `AZPath`'s, and `test_az.py` and `test_az_fake.py`
run it through both."""

from __future__ import annotations

import pickle
import types

import pytest

pytest.importorskip("uripath")
pytest.importorskip("objpath.uri")
pytest.importorskip("azure.storage.blob")

from pathlib_next.mempath import MemPath
from uripath import URIPath

from objpath import AZBackend, AZPath, BaseAZBackend
from objpath import _az_backend
from objpath.exceptions import UnreachableKeysError
from objpath.uri import AZURIPath
from test_az_fake import (
    _FakeBackend,
    _Forbidden,
    _Missing,
    _ServiceUnavailable,
    _container_with,
)


class _PicklableBackend(BaseAZBackend):
    picklable = True
    account = "carried"

    def client(self):
        raise AssertionError("a path must not ask for the client")


@pytest.fixture(autouse=True)
def _fresh_default_backends():
    """The default backends are cached per account for the process; a test
    starts with none and leaves none."""
    kept = dict(_az_backend._DEFAULT_BACKENDS)
    _az_backend._DEFAULT_BACKENDS.clear()
    yield
    _az_backend._DEFAULT_BACKENDS.clear()
    _az_backend._DEFAULT_BACKENDS.update(kept)


def _containers(*names):
    """A fake backend whose account holds the containers `names`, and the list
    of the keyword arguments each `list_containers()` call was made with."""
    backend = _FakeBackend()
    calls = []

    def list_containers(**kwargs):
        calls.append(kwargs)
        return iter([types.SimpleNamespace(name=name) for name in names])

    backend.client_obj.list_containers = list_containers
    return backend, calls


# --- the URL ----------------------------------------------------------------


def test_a_url_of_the_az_scheme_is_an_az_uri_path():
    path = URIPath("az://account/container/key/path")
    assert type(path) is AZURIPath
    assert not isinstance(path, AZPath)
    assert str(path) == "az://account/container/key/path"


@pytest.mark.parametrize(
    "url, account, container, key",
    [
        ("az://acct/cont/dir/key", "acct", "cont", "dir/key"),
        ("az://acct/cont/dir/", "acct", "cont", "dir"),
        ("az://acct/cont/dir//", "acct", "cont", "dir/"),
        ("az://acct/cont/a//b", "acct", "cont", "a//b"),
        ("az://acct/cont/", "acct", "cont", ""),
        ("az://acct/cont", "acct", "cont", ""),
        ("az://acct/", "acct", "", ""),
        ("az://acct", "acct", "", ""),
        ("az:///cont/key", "", "cont", "key"),
    ],
)
def test_the_account_the_container_and_the_key_are_parts_of_the_url(
    url, account, container, key
):
    path = URIPath(url)
    assert path.account == account
    assert path.container == container
    assert path.key == key
    assert str(path) == url


def test_a_path_below_a_container_keeps_the_account_and_the_container():
    path = URIPath("az://acct/cont/dir/key")
    for derived in (
        path.parent,
        path / "more",
        path.with_name("other"),
        path.parents[0],
    ):
        assert derived.account == "acct"
        assert derived.container == "cont"
    assert path.parent.key == "dir"
    assert (path / "more").key == "dir/key/more"
    assert path.parents[-1].container == ""


def test_a_uri_path_is_not_local():
    assert not URIPath("az://acct/cont/key").is_local()


# --- the account is a directory of containers --------------------------------


@pytest.mark.parametrize("url", ["az://account", "az://account/"])
def test_the_account_lists_its_containers_as_directories(url):
    backend, calls = _containers("logs", "data")
    account = URIPath(url, backend=backend)
    assert account.container == ""
    assert account.is_dir()
    children = {child.name: child for child in account.iterdir()}
    assert sorted(children) == ["data", "logs"]
    assert all(isinstance(child, AZURIPath) for child in children.values())
    assert {child.container for child in children.values()} == {"data", "logs"}
    assert str(children["logs"]) == "az://account/logs"
    # Each child carries the stat of the listing: it asks nothing more.
    assert all(child.is_dir() for child in children.values())
    assert calls == [{"results_per_page": 1}, {}]


def test_an_account_with_no_container_is_still_a_directory():
    backend, _calls = _containers()
    account = URIPath("az://account", backend=backend)
    assert account.is_dir()
    assert list(account.iterdir()) == []


def test_the_account_asks_the_service_whether_it_exists():
    backend, _calls = _containers()
    calls = []

    def missing(**kwargs):
        calls.append(kwargs)
        raise _Missing("AccountNotFound")

    backend.client_obj.list_containers = missing
    account = URIPath("az://account", backend=backend)
    with pytest.raises(FileNotFoundError):
        account.stat()
    assert not account.exists()
    assert calls and all(kwargs == {"results_per_page": 1} for kwargs in calls)

    def forbidden(**kwargs):
        raise _Forbidden("AuthorizationFailure")

    backend.client_obj.list_containers = forbidden
    with pytest.raises(PermissionError) as info:
        account.stat()
    assert info.value.filename == "az://account"
    assert info.value.__cause__ is None

    def unavailable(**kwargs):
        raise _ServiceUnavailable("503")

    backend.client_obj.list_containers = unavailable
    with pytest.raises(OSError) as info:
        list(account.iterdir())
    assert not isinstance(info.value, FileNotFoundError)

    backend.client_obj.list_containers = lambda **kwargs: iter(["container"])
    assert account.is_dir()


def test_an_account_is_a_directory_to_read_or_write():
    backend, calls = _containers("logs")
    account = URIPath("az://account", backend=backend)
    with pytest.raises(IsADirectoryError):
        account.read_bytes()
    with pytest.raises(IsADirectoryError):
        account.write_bytes(b"x")
    with pytest.raises(IsADirectoryError):
        account.open("r+b")
    with pytest.raises(IsADirectoryError) as info:
        account.open("xb")
    assert info.value.filename == "az://account"
    assert calls == []


def test_an_account_cannot_be_unlinked_removed_or_created_again():
    backend, calls = _containers("logs")
    account = URIPath("az://account", backend=backend)
    with pytest.raises(IsADirectoryError):
        account.unlink()
    with pytest.raises(PermissionError) as info:
        account.rmdir()
    assert info.value.filename == "az://account"
    with pytest.raises(FileExistsError):
        account.mkdir()
    with pytest.raises(IsADirectoryError):
        account.checksum()
    assert calls == [{"results_per_page": 1}, {"results_per_page": 1}]


def test_a_container_below_the_account_lists_its_blobs_and_prefixes():
    backend, _calls = _containers("container")
    backend.client_obj.container.objects.update({"a.txt": b"a", "d/b.txt": b"b"})
    names = {
        child.name: child.is_dir()
        for child in (URIPath("az://account", backend=backend) / "container").iterdir()
    }
    assert names == {"a.txt": False, "d": True}


# --- a URL without an account ------------------------------------------------


@pytest.mark.parametrize("url", ["az:x", "az:///c/x"])
def test_a_url_without_an_account_does_not_exist_and_builds_no_credential(
    url, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("a credential must not be built")

    monkeypatch.setattr(_az_backend, "_default_credential", forbidden)
    path = URIPath(url)
    assert path.exists() is False
    with pytest.raises(FileNotFoundError, match="account"):
        path.stat()
    with pytest.raises(FileNotFoundError, match="account"):
        path.read_bytes()
    with pytest.raises(FileNotFoundError, match="account"):
        path.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="account"):
        list(path.iterdir())
    assert _az_backend._DEFAULT_BACKENDS == {}


# --- the backend ------------------------------------------------------------


def test_without_a_backend_the_paths_of_an_account_share_its_default_one():
    first = URIPath("az://acct/one/k")
    second = URIPath("az://acct/two/j")
    other = URIPath("az://elsewhere/one/k")
    assert isinstance(first.backend, AZBackend)
    assert first.backend.account == "acct"
    assert second.backend is first.backend
    assert other.backend is not first.backend
    # The backend `AZPath(account=...)` shares.
    assert first.backend is AZPath("k", container="one", account="acct").backend


def test_a_default_backend_serves_the_paths_of_its_account():
    backend, container = _container_with(**{"a.txt": b"content"})
    _az_backend._DEFAULT_BACKENDS["account"] = backend
    assert URIPath("az://account/container/a.txt").read_bytes() == b"content"
    URIPath("az://account/container/b.txt").write_bytes(b"new")
    assert container.objects["b.txt"] == b"new"


def test_a_supplied_backend_is_used_as_it_is_for_every_path():
    backend, container = _container_with(**{"a.txt": b"content"})
    path = URIPath("az://account/container/a.txt", backend=backend)
    assert path.backend is backend
    assert path.read_bytes() == b"content"
    # Whatever account or container the URL names.
    elsewhere = URIPath("az://other/container/a.txt", backend=backend)
    assert elsewhere.backend is backend
    assert elsewhere.read_bytes() == b"content"
    assert _az_backend._DEFAULT_BACKENDS == {}


def test_paths_made_from_a_path_with_a_supplied_backend_keep_it():
    backend, _container = _container_with(**{"d/a.txt": b"a"})
    path = URIPath("az://account/container/d", backend=backend)
    for derived in (
        path / "a.txt",
        path.parent,
        path.with_name("e"),
        *path.iterdir(),
    ):
        assert derived.backend is backend


# --- copy and rename --------------------------------------------------------


def test_a_copy_inside_one_container_is_the_service_copy():
    backend, container = _container_with(**{"a.txt": b"content"})
    source = URIPath("az://account/container/a.txt", backend=backend)
    source.copy(URIPath("az://account/container/b.txt", backend=backend))
    source.copy("c.txt")
    source.copy("az://account/container/d.txt")
    assert container.copies == [
        ("a.txt", "b.txt"),
        ("a.txt", "c.txt"),
        ("a.txt", "d.txt"),
    ]
    assert container.downloads == [] and container.uploads == []
    assert container.objects["d.txt"] == b"content"


def test_a_service_copy_reports_progress_with_the_source_path():
    backend, _container = _container_with(**{"a.txt": b"content"})
    source = URIPath("az://account/container/a.txt", backend=backend)
    calls = []
    source.copy(
        URIPath("az://account/container/b.txt", backend=backend),
        progress=lambda path, copied, total: calls.append((path, copied, total)),
    )
    ((path, copied, total),) = calls
    assert path is source
    assert (copied, total) == (7, 7)


@pytest.mark.parametrize(
    "target, where",
    [
        ("az://account/elsewhere/b.txt", "elsewhere"),
        ("az://other/container/b.txt", "container"),
    ],
    ids=["other-container", "other-account"],
)
def test_a_copy_to_another_container_or_account_is_streamed(target, where):
    backend, container = _container_with(**{"a.txt": b"content"})
    URIPath("az://account/container/a.txt", backend=backend).copy(
        URIPath(target, backend=backend)
    )
    assert backend.client_obj.containers[where].objects["b.txt"] == b"content"
    assert container.copies == [] and container.downloads == ["a.txt"]


def test_a_copy_between_two_supplied_backends_is_streamed():
    source_backend, source_container = _container_with(**{"a.txt": b"content"})
    target_backend, target_container = _container_with()
    URIPath("az://account/container/a.txt", backend=source_backend).copy(
        URIPath("az://account/container/b.txt", backend=target_backend)
    )
    assert target_container.objects == {"b.txt": b"content"}
    assert source_container.copies == [] and target_container.copies == []
    assert source_container.downloads == ["a.txt"]


def test_a_copy_between_two_paths_on_the_default_backend_of_one_account():
    backend, container = _container_with(**{"a.txt": b"content"})
    _az_backend._DEFAULT_BACKENDS["account"] = backend
    URIPath("az://account/container/a.txt").copy(
        URIPath("az://account/container/b.txt")
    )
    assert container.copies == [("a.txt", "b.txt")]


def test_a_copy_between_the_default_backends_of_two_accounts_is_streamed():
    backend, container = _container_with(**{"a.txt": b"content"})
    other_backend, other_container = _container_with()
    _az_backend._DEFAULT_BACKENDS["account"] = backend
    _az_backend._DEFAULT_BACKENDS["other"] = other_backend
    URIPath("az://account/container/a.txt").copy(URIPath("az://other/container/b.txt"))
    assert other_container.objects == {"b.txt": b"content"}
    assert container.copies == [] and other_container.copies == []


def test_a_copy_to_a_path_of_another_class_is_streamed():
    backend, container = _container_with(**{"a.txt": b"content"})
    target = MemPath("/copy.txt")
    URIPath("az://account/container/a.txt", backend=backend).copy(target)
    assert target.read_bytes() == b"content"
    assert container.copies == []


def test_rename_inside_one_container_is_a_service_copy_and_a_delete():
    backend, container = _container_with(**{"a.txt": b"content"})
    source = URIPath("az://account/container/a.txt", backend=backend)
    moved = source.rename(URIPath("az://account/container/b.txt", backend=backend))
    assert str(moved) == "az://account/container/b.txt"
    assert container.objects == {"b.txt": b"content"}
    assert container.copies == [("a.txt", "b.txt")]
    renamed = moved.rename("c.txt")
    assert str(renamed) == "az://account/container/c.txt"
    assert container.objects == {"c.txt": b"content"}


@pytest.mark.parametrize(
    "target",
    ["az://account/elsewhere/b.txt", "az://other/container/b.txt"],
    ids=["other-container", "other-account"],
)
def test_rename_across_containers_is_not_implemented_and_move_copies(target):
    backend, container = _container_with(**{"a.txt": b"content"})
    source = URIPath("az://account/container/a.txt", backend=backend)
    destination = URIPath(target, backend=backend)
    with pytest.raises(NotImplementedError):
        source.rename(destination)
    assert container.objects == {"a.txt": b"content"} and container.copies == []

    source.move(destination)
    assert container.copies == []
    assert "a.txt" not in container.objects
    holder = (
        backend.client_obj.containers["elsewhere"]
        if "elsewhere" in target
        else container
    )
    assert holder.objects == {"b.txt": b"content"}


# --- callbacks get URI paths ---------------------------------------------------


def test_the_callback_of_rm_gets_the_uri_path_of_the_blob_that_failed():
    backend, container = _container_with(**{"dir/a.txt": b"a", "dir/b.txt": b"b"})
    container.delete_errors.add("dir/a.txt")
    offered = []
    URIPath("az://account/container/dir", backend=backend).rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append((type(error), where)) or True,
    )
    ((kind, where),) = offered
    assert kind is OSError
    assert isinstance(where, AZURIPath)
    assert str(where) == "az://account/container/dir/a.txt"
    assert (where.account, where.container, where.key) == (
        "account",
        "container",
        "dir/a.txt",
    )
    assert where.backend is backend


def test_the_callback_of_rm_gets_the_prefix_for_a_refusal():
    backend, container = _container_with(**{"d/x.txt": b"x", "d//y.txt": b"y"})
    prefix = URIPath("az://account/container/d", backend=backend)
    offered = []
    prefix.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append((error, where)) or True,
    )
    ((error, where),) = offered
    assert isinstance(error, UnreachableKeysError)
    assert isinstance(where, AZURIPath)
    assert where == prefix
    assert str(where) == "az://account/container/d"
    assert sorted(container.objects) == ["d//y.txt"]


def test_the_callbacks_of_copy_get_the_uri_path_of_the_blob_that_failed():
    backend, container = _container_with(**{"dir/a.txt": b"a", "dir/b.txt": b"b"})

    def failing(name):
        if name == "dir/b.txt":
            raise _ServiceUnavailable("503")

    container.before_read = failing
    target = MemPath("/out")
    offered, progressed = [], []
    URIPath("az://account/container/dir", backend=backend).copy(
        target,
        recursive=True,
        on_error=lambda error, where: offered.append((type(error), where)) or True,
        progress=lambda where, copied, total: progressed.append(where),
    )
    ((kind, where),) = offered
    assert kind is OSError
    assert isinstance(where, AZURIPath)
    assert str(where) == "az://account/container/dir/b.txt"
    assert (target / "a.txt").read_bytes() == b"a"
    assert not (target / "b.txt").exists()
    assert progressed and all(isinstance(path, AZURIPath) for path in progressed)
    assert {str(path) for path in progressed} == {"az://account/container/dir/a.txt"}


# --- pickling ---------------------------------------------------------------


def test_a_pickled_uri_path_is_the_same_url():
    path = URIPath("az://acct/cont/dir/key")
    restored = pickle.loads(pickle.dumps(path))
    assert type(restored) is AZURIPath
    assert restored == path
    assert (restored.account, restored.container, restored.key) == (
        "acct",
        "cont",
        "dir/key",
    )
    assert restored.backend is path.backend


def test_a_pickled_uri_path_leaves_a_supplied_backend_and_its_options_behind():
    backend = AZBackend(connection_string="AccountName=acct;AccountKey=SECRETKEY")
    path = URIPath("az://acct/cont/key", backend=backend)
    data = pickle.dumps(path)
    assert b"SECRETKEY" not in data
    restored = pickle.loads(data)
    assert restored == path
    assert restored.backend is not backend
    assert restored.backend is AZPath("x", container="cont", account="acct").backend
    assert restored.backend.client_kwargs == {}


def test_a_backend_that_declares_itself_picklable_travels_with_the_uri_path():
    path = URIPath("az://carried/cont/key", backend=_PicklableBackend())
    restored = pickle.loads(pickle.dumps(path))
    assert restored == path
    assert type(restored.backend) is _PicklableBackend
    assert restored.backend is not path.backend
