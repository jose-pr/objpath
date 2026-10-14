"""`AZPath`, the path of an Azure Blob Storage container that needs no URI: what
its constructor accepts and refuses, which backend a path gets, how a path is
named, compared and printed, and what a pickled path carries. Nothing here
reaches the network: a path connects when it is used, not when it is built."""

from __future__ import annotations

import pickle
import sys
import types

import pytest

from objpath import AZBackend, AZPath, BaseAZBackend
from objpath import _az_backend

ACCOUNT_URL = "https://acct.blob.core.windows.net"


class _NamedBackend(BaseAZBackend):
    """A backend of an account that does not travel with a pickled path."""

    account = "named"

    def client(self):
        raise AssertionError("a path must not ask for the client")


class _AnonymousBackend(BaseAZBackend):
    def client(self):
        raise AssertionError("a path must not ask for the client")


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


@pytest.fixture
def built_clients(monkeypatch):
    """Replaces `BlobServiceClient` with a recorder of how it was built:
    `("init", account_url, credential, options)` or
    `("conn", connection_string, credential, options)`."""
    blob = pytest.importorskip("azure.storage.blob")
    built = []

    class BlobServiceClient:
        def __init__(self, account_url, credential=None, **options):
            built.append(("init", account_url, credential, options))

        @classmethod
        def from_connection_string(cls, conn_str, credential=None, **options):
            built.append(("conn", conn_str, credential, options))
            return object.__new__(cls)

    monkeypatch.setattr(blob, "BlobServiceClient", BlobServiceClient)
    return built


@pytest.fixture
def default_credential(monkeypatch):
    """Replaces azure-identity's `DefaultAzureCredential` with a class whose
    instances are recorded in the list this fixture yields."""
    identity = types.ModuleType("azure.identity")
    made = []

    class DefaultAzureCredential:
        def __init__(self):
            made.append(self)

    identity.DefaultAzureCredential = DefaultAzureCredential
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    return made


def _path(*segments, **options):
    options.setdefault("container", "c")
    if not ({"backend", "account"} & set(options)) and not any(
        isinstance(segment, AZPath) for segment in segments
    ):
        options.setdefault("account", "a")
    return AZPath(*segments, **options)


# --- the container ----------------------------------------------------------


def test_a_container_is_required():
    with pytest.raises(TypeError, match="container"):
        AZPath("key", account="acct")
    with pytest.raises(TypeError, match="container"):
        AZPath(account="acct")


def test_a_path_joined_from_another_takes_its_container():
    base = AZPath("dir", container="cont", account="acct")
    joined = AZPath(base, "key")
    assert (joined.container, joined.key) == ("cont", "dir/key")
    assert joined.account == "acct"


def test_container_moves_a_joined_path_to_another_container():
    base = AZPath("dir", container="cont", account="acct")
    moved = AZPath(base, "key", container="other")
    assert (moved.container, moved.key) == ("other", "dir/key")
    assert moved.backend is base.backend
    assert moved != AZPath(base, "key")


def test_container_must_be_a_name():
    with pytest.raises(TypeError, match="container"):
        AZPath("key", container=3, account="acct")
    for bad in ("", "a/b"):
        with pytest.raises(ValueError, match="container"):
            AZPath("key", container=bad, account="acct")


def test_account_must_be_a_name():
    with pytest.raises(TypeError, match="account"):
        AZPath("key", container="c", account=3)
    for bad in ("", "a/b"):
        with pytest.raises(ValueError, match="account"):
            AZPath("key", container="c", account=bad)


def test_the_container_of_a_derived_path_is_the_container_of_its_source():
    base = AZPath("dir/sub/key.txt", container="cont", account="acct")
    for derived in (
        base / "more",
        base.joinpath("more", "again"),
        base.parent,
        base.parent.parent,
        base.with_name("other.txt"),
        base.with_suffix(".md"),
    ):
        assert derived.container == "cont"
        assert derived.backend is base.backend
        assert type(derived) is AZPath


# --- the backend ------------------------------------------------------------


def test_account_builds_the_default_backend_of_that_account(built_clients):
    first = AZPath("a", container="one", account="acct")
    second = AZPath("b", container="two", account="acct")
    other = AZPath("a", container="one", account="elsewhere")
    assert isinstance(first.backend, AZBackend)
    assert first.backend.account == "acct"
    assert first.account == "acct"
    # One backend (one client, one credential) per account for the process.
    assert second.backend is first.backend
    assert other.backend is not first.backend
    assert other.backend.account == "elsewhere"
    assert sorted(_az_backend._DEFAULT_BACKENDS) == ["acct", "elsewhere"]
    assert built_clients == []


def test_a_path_joined_from_another_keeps_its_backend(built_clients):
    base = AZPath("dir", container="cont", account="acct")
    for derived in (AZPath(base, "key"), base / "key", AZPath(base)):
        assert derived.backend is base.backend
    assert built_clients == []


def test_account_with_a_joined_path_uses_the_default_backend_of_that_account():
    base = AZPath("dir", container="cont", account="acct")
    other = AZPath(base, "key", account="elsewhere")
    assert other.backend is not base.backend
    assert other.backend is AZPath("x", container="cont", account="elsewhere").backend
    assert other.container == "cont"


def test_backend_supplies_the_backend_as_it_is():
    backend = _NamedBackend()
    first = AZPath("a", container="one", backend=backend)
    second = AZPath("b", container="two", backend=backend)
    assert first.backend is backend and second.backend is backend
    assert first.account == "named"
    assert _az_backend._DEFAULT_BACKENDS == {}


@pytest.mark.parametrize(
    "options",
    [
        {"account": "acct"},
        {"credential": "token"},
        {"connection_string": "AccountName=acct"},
        {"account_url": ACCOUNT_URL},
        {"retry_total": 0},
        {"account": "acct", "credential": "token"},
    ],
    ids=lambda options: "+".join(sorted(options)),
)
def test_backend_together_with_account_or_client_options_is_a_type_error(options):
    with pytest.raises(TypeError, match="backend="):
        AZPath("key", container="c", backend=_AnonymousBackend(), **options)
    base = AZPath("key", container="c", account="acct")
    with pytest.raises(TypeError, match="backend="):
        AZPath(base, "more", backend=_AnonymousBackend(), **options)


def test_no_account_backend_or_options_and_no_joined_path_is_a_type_error():
    with pytest.raises(TypeError, match="account="):
        AZPath("key", container="c")
    with pytest.raises(TypeError, match="account="):
        AZPath(container="c")
    assert _az_backend._DEFAULT_BACKENDS == {}


def test_client_options_build_the_backend_of_the_path(built_clients):
    path = AZPath("key", container="c", credential="token", account_url=ACCOUNT_URL)
    assert isinstance(path.backend, AZBackend)
    assert path.backend.client_kwargs == {
        "credential": "token",
        "account_url": ACCOUNT_URL,
    }
    # Its own: not the default backend, and not shared with a path built the
    # same way.
    again = AZPath("key", container="c", credential="token", account_url=ACCOUNT_URL)
    assert again.backend is not path.backend
    assert _az_backend._DEFAULT_BACKENDS == {}
    assert built_clients == []
    path.backend.client()
    assert built_clients == [("init", ACCOUNT_URL, "token", {})]


def test_account_with_a_credential_builds_the_backend_of_the_path(
    built_clients, default_credential
):
    path = AZPath("key", container="c", account="acct", credential="token")
    assert path.account == "acct"
    assert path.backend is not AZPath("key", container="c", account="acct").backend
    path.backend.client()
    # The account names the endpoint, and the credential given is the one used.
    assert built_clients == [("init", ACCOUNT_URL, "token", {})]
    assert default_credential == []


@pytest.mark.parametrize(
    "options, account",
    [
        (
            {
                "connection_string": "DefaultEndpointsProtocol=https;"
                "AccountName=acct;AccountKey=a2V5;EndpointSuffix=core.windows.net"
            },
            "acct",
        ),
        ({"connection_string": "accountname=lower;AccountKey=a2V5"}, "lower"),
        ({"account_url": "https://one.blob.core.windows.net"}, "one"),
        ({"account_url": "https://two.blob.core.windows.net/"}, "two"),
        (
            {"account_url": "http://127.0.0.1:10000/devstoreaccount1"},
            "devstoreaccount1",
        ),
        ({"account_url": "http://127.0.0.1:10000/emu?sv=1&sig=SECRET"}, "emu"),
        ({"credential": "token"}, None),
        ({"retry_total": 0}, None),
    ],
    ids=[
        "connection-string",
        "connection-string-lower-case",
        "blob-endpoint",
        "blob-endpoint-slash",
        "emulator",
        "emulator-sas",
        "credential-only",
        "other-option-only",
    ],
)
def test_the_account_is_learned_from_the_connection_string_or_account_url(
    options, account
):
    path = AZPath("key", container="c", **options)
    assert path.account == account
    assert path.backend.account == account


def test_account_given_with_client_options_wins_over_the_one_they_name():
    path = AZPath(
        "key",
        container="c",
        account="given",
        connection_string="AccountName=named;AccountKey=a2V5",
    )
    assert path.account == "given"


def test_client_options_on_a_joined_path_build_another_backend():
    base = AZPath("dir", container="cont", account="acct")
    other = AZPath(base, "key", credential="token", account_url=ACCOUNT_URL)
    assert other.backend is not base.backend
    assert other.backend.client_kwargs["credential"] == "token"
    assert other.container == "cont"


def test_a_path_that_builds_a_client_option_backend_does_not_connect(built_clients):
    paths = [
        AZPath("a", container="c", account="acct"),
        AZPath("a", container="c", account="acct", credential="token"),
        AZPath("a", container="c", connection_string="AccountName=x;AccountKey=a2V5"),
        AZPath("a", container="c", backend=_NamedBackend()),
    ]
    # Pure-path work needs no client.
    for path in paths:
        assert path.parent.name == ""
        assert (path / "b").name == "b"
        assert str(path) and repr(path)
        assert path == path / "."
    assert built_clients == []


def test_the_default_backend_authenticates_with_the_default_credential(
    built_clients, default_credential
):
    backend = AZPath("key", container="c", account="otheracct").backend
    assert default_credential == []
    backend.client()
    ((kind, url, credential, options),) = built_clients
    assert (kind, url, options) == (
        "init",
        "https://otheracct.blob.core.windows.net",
        {},
    )
    assert credential is default_credential[0]


def test_the_default_backend_without_azure_identity_raises_when_used(
    built_clients, monkeypatch
):
    # An entry of None in `sys.modules` makes the import of that name fail.
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    path = AZPath("key", container="c", account="myacct")
    with pytest.raises(ImportError, match="azure-identity"):
        path.read_bytes()
    assert built_clients == []


def test_a_core_path_is_not_local():
    assert not AZPath("key", container="c", account="acct").is_local()


# --- the name ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, key",
    [
        ("dir", "dir"),
        ("dir/", "dir"),
        ("dir//", "dir/"),
        ("dir///", "dir//"),
        ("a//b", "a//b"),
        ("a//b/", "a//b"),
        ("/a", "a"),
        ("//a", "a"),
        ("a/./b", "a/b"),
        ("./a", "a"),
        ("a/.", "a"),
        ("../a", "../a"),
        ("a/../b", "a/../b"),
        ("", ""),
        (".", ""),
        ("/", ""),
    ],
)
def test_the_key_drops_one_trailing_slash_and_keeps_interior_empty_names(text, key):
    assert _path(text).key == key


def test_a_slash_that_ends_a_piece_separates_it_from_the_next():
    assert _path("a/", "b").key == "a/b"
    assert _path("a//", "b").key == "a//b"
    assert _path("a", "b/", "c").key == "a/b/c"


def test_an_absolute_piece_replaces_what_came_before_it():
    assert _path("a", "/b").key == "b"
    assert _path("a", "/b", "c").key == "b/c"


def test_x_and_slash_x_are_the_same_key():
    relative, rooted = _path("x"), _path("/x")
    assert relative.key == rooted.key == "x"
    assert relative == rooted
    assert hash(relative) == hash(rooted)
    assert len({relative, rooted}) == 1
    assert not relative.is_absolute()
    assert rooted.is_absolute()


def test_spellings_of_one_key_are_one_path():
    spellings = [_path(text) for text in ("a/b", "a/b/", "./a/b", "/a/b", "a/./b")]
    assert len({*spellings}) == 1
    assert len({hash(path) for path in spellings}) == 1


def test_a_key_with_a_trailing_slash_is_not_the_key_without():
    assert _path("dir//") != _path("dir")
    assert _path("a//b") != _path("a/b")
    assert len({_path("dir//"), _path("dir"), _path("a//b"), _path("a/b")}) == 4


def test_paths_of_two_containers_are_different_paths():
    in_one = AZPath("x", container="one", account="a")
    in_two = AZPath("x", container="two", account="a")
    assert in_one != in_two
    assert len({in_one, in_two}) == 2
    assert in_one == AZPath("x", container="one", account="a")
    assert in_one.key == in_two.key


def test_a_path_equals_no_other_kind_of_object():
    path = _path("x")
    assert path != "x"
    assert path != "az://a/c/x"
    assert path != ("c", "x")
    assert path != None  # noqa: E711


# --- the URL ----------------------------------------------------------------


def test_str_is_the_url_of_the_blob():
    assert str(_path("dir/key")) == "az://a/c/dir/key"
    assert _path("dir/key").as_uri() == "az://a/c/dir/key"
    assert str(_path("dir/key", account="other")) == "az://other/c/dir/key"
    assert str(_path("a//b")) == "az://a/c/a//b"
    assert str(_path("dir//")) == "az://a/c/dir//"


def test_the_url_of_the_container_is_the_one_with_no_key():
    assert str(_path()) == "az://a/c/"
    assert str(_path("/")) == "az://a/c/"


def test_the_url_escapes_what_a_url_cannot_hold():
    path = _path("a b/ü?#%")
    assert path.as_uri() == "az://a/c/a%20b/%C3%BC%3F%23%25"
    assert path.key == "a b/ü?#%"


def test_str_of_a_path_whose_backend_names_no_account_has_an_empty_authority():
    path = AZPath("dir/key", container="c", backend=_AnonymousBackend())
    assert path.account is None
    assert str(path) == "az:///c/dir/key"
    with pytest.raises(NotImplementedError, match="account"):
        path.as_uri()


def test_repr_names_the_container_and_the_account_of_a_default_backend():
    path = AZPath("dir/key", container="c", account="acct")
    assert repr(path) == "AZPath('dir/key', container='c', account='acct')"


def test_repr_of_a_path_with_its_own_backend_shows_the_backend():
    backend = _NamedBackend()
    path = AZPath("dir/key", container="c", backend=backend)
    assert repr(path) == f"AZPath('dir/key', container='c', backend={backend!r})"
    own = AZPath("k", container="c", account="acct", credential="token")
    assert repr(own).startswith("AZPath('k', container='c', backend=AZBackend(")
    anonymous = AZPath("k", container="c", backend=_AnonymousBackend())
    assert "backend=" in repr(anonymous) and "account=" not in repr(anonymous)


def test_neither_repr_nor_str_shows_a_credential():
    path = AZPath(
        "k",
        container="c",
        credential="SECRETTOKEN",
        connection_string="AccountName=acct;AccountKey=SECRETKEY",
    )
    for text in (repr(path), str(path), repr(path.backend)):
        assert "SECRETTOKEN" not in text and "SECRETKEY" not in text


# --- pickling ---------------------------------------------------------------


def _roundtrip(path):
    return pickle.loads(pickle.dumps(path))


@pytest.mark.parametrize("text", ["dir/key", "dir//", "a//b", "/rooted", "", "/"])
def test_a_pickled_path_carries_its_container_its_names_and_its_account(text):
    path = AZPath(text, container="cont", account="acct")
    restored = _roundtrip(path)
    assert restored == path
    assert restored.container == "cont"
    assert restored.key == path.key
    assert restored.is_absolute() == path.is_absolute()
    assert restored.account == "acct"
    # The default backend of the account, which the receiving process builds
    # for itself.
    assert restored.backend is path.backend


def test_a_pickled_path_carries_no_client_option():
    path = AZPath(
        "key",
        container="c",
        connection_string="AccountName=acct;AccountKey=SECRETKEY",
        credential="SECRETTOKEN",
        retry_total=7,
    )
    data = pickle.dumps(path)
    assert b"SECRETKEY" not in data and b"SECRETTOKEN" not in data
    restored = pickle.loads(data)
    assert restored == path
    assert restored.account == "acct"
    # The account is all that travels: the receiving process authenticates with
    # the default credential of that account, not with the options of the sender.
    assert restored.backend is not path.backend
    assert restored.backend is AZPath("x", container="c", account="acct").backend
    assert restored.backend.client_kwargs == {}


def test_a_backend_that_declares_itself_picklable_travels_with_its_path():
    path = AZPath("key", container="c", backend=_PicklableBackend())
    restored = _roundtrip(path)
    assert restored == path
    assert type(restored.backend) is _PicklableBackend
    assert restored.backend is not path.backend
    assert _az_backend._DEFAULT_BACKENDS == {}


def test_a_backend_that_does_not_travel_is_replaced_by_the_default_of_its_account():
    path = AZPath("key", container="c", backend=_NamedBackend())
    restored = _roundtrip(path)
    assert restored == path
    assert restored.account == "named"
    assert type(restored.backend) is AZBackend
    assert restored.backend is _az_backend._DEFAULT_BACKENDS["named"]


def test_a_path_with_no_account_and_a_backend_that_does_not_travel_cannot_be_pickled():
    path = AZPath("key", container="c", backend=_AnonymousBackend())
    with pytest.raises(TypeError, match="account"):
        pickle.dumps(path)


def test_paths_of_two_accounts_with_one_container_and_key_are_not_equal():
    first = AZPath("dir/key", container="cont", account="one")
    second = AZPath("dir/key", container="cont", account="two")
    assert first != second
    assert len({first, second}) == 2
    assert first == AZPath("/dir/key", container="cont", account="one")
    assert sorted([second, first]) == [first, second]
