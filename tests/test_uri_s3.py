"""What is only about the `s3:` URI: `URIPath("s3://bkt/k")` is an
`S3URIPath` over an `S3Path`, the bucket and the key come out of the URL, a
backend is supplied for every bucket a URL names, and the copy, rename and
callbacks are decided from URIs. The I/O behaviour both classes share is in
`test_s3.py`."""

from __future__ import annotations

import errno
import pickle
import pytest

pytest.importorskip("uripath")
pytest.importorskip("objpath.uri")
botocore = pytest.importorskip("botocore")
import botocore.exceptions as _botoexc

from uripath import URIPath

from objpath import BaseS3Backend, S3Backend, S3Path
from objpath import _s3_backend
from objpath.uri import S3URIPath
from pathlib_next.mempath import MemPath

# boto3 announces the end of its Python 3.9 support when it creates its first
# client.
pytestmark = pytest.mark.filterwarnings(r"ignore:.*Python 3\.9.*:Warning:boto3(\..*)?$")


class _CountingBackend(BaseS3Backend):
    """A backend that counts how often a client is asked for."""

    def __init__(self):
        self.asked = 0

    def client(self):
        self.asked += 1
        raise AssertionError("a request was sent")


class _PicklableBackend(BaseS3Backend):
    picklable = True

    def __init__(self, tag):
        self.tag = tag

    def client(self):
        raise AssertionError("a request was sent")


@pytest.fixture
def moto_s3(aws_test_credentials):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        client.create_bucket(Bucket="other")
        yield client


def _keys(client, bucket="bkt"):
    listing = client.list_objects_v2(Bucket=bucket).get("Contents", [])
    return sorted(obj["Key"] for obj in listing)


def _stored(client, key, bucket="bkt"):
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _record(backend):
    """The names of the requests that `backend`'s client sends from now on."""
    sent = []
    events = backend.client().meta.events
    events.register("before-call.s3", lambda model, **_: sent.append(model.name))
    return sent


def _refuse(backend, operation, code):
    """Makes `backend`'s client answer `operation` with the error `code`."""
    client = backend.client()
    original = client._make_api_call

    def refusing(name, params):
        if name == operation:
            raise _botoexc.ClientError(
                {"Error": {"Code": code, "Message": code}}, operation
            )
        return original(name, params)

    client._make_api_call = refusing


# --- the scheme and the parts of a URL ---------------------------------------------


def test_an_s3_url_is_an_s3_uri_path():
    from uripath import URIPath as Base

    path = URIPath("s3://bucket/a")
    assert type(path) is S3URIPath
    assert isinstance(path, Base)
    assert not isinstance(path, S3Path)


def test_the_bucket_and_the_key_are_taken_from_the_url():
    path = URIPath("s3://bucket/docs/readme.txt")
    assert path.bucket == "bucket"
    assert path.key == "docs/readme.txt"
    assert str(path) == "s3://bucket/docs/readme.txt"


def test_a_url_with_a_bucket_and_no_path_names_the_bucket_itself():
    for url in ("s3://bucket", "s3://bucket/"):
        path = URIPath(url)
        assert path.bucket == "bucket"
        assert path.key == ""


@pytest.mark.parametrize(
    "url, key",
    [
        ("s3://bucket/dir/", "dir"),
        ("s3://bucket/dir", "dir"),
        ("s3://bucket/a/b/", "a/b"),
        ("s3://bucket/", ""),
        # Exactly one trailing slash goes; interior empty segments are
        # literal key bytes and stay.
        ("s3://bucket/a//b", "a//b"),
        ("s3://bucket/dir//", "dir/"),
        # The key is the decoded path.
        ("s3://bucket/a%20b%23c%3Fd", "a b#c?d"),
    ],
)
def test_the_key_of_a_url_drops_one_trailing_slash_and_is_decoded(url, key):
    assert URIPath(url).key == key


def test_a_path_joined_from_a_url_keeps_its_bucket_and_backend():
    own = _CountingBackend()
    base = URIPath("s3://bucket/dir", backend=own)
    for path in (base / "key", base.parent, base.with_name("other")):
        assert path.bucket == "bucket"
        assert path.backend is own
    assert (base / "key").key == "dir/key"


def test_the_same_url_is_the_same_path_and_a_path_of_the_core_class_is_another():
    assert URIPath("s3://bkt/k") == URIPath("s3://bkt/k")
    assert hash(URIPath("s3://bkt/k")) == hash(URIPath("s3://bkt/k"))
    assert URIPath("s3://bkt/k") != URIPath("s3://other/k")
    assert URIPath("s3://bkt/k") != S3Path("k", bucket="bkt")


# --- a URL without a bucket ----------------------------------------------------------


@pytest.mark.parametrize("url", ["s3:x", "s3:///x", "s3:///"])
def test_a_url_without_a_bucket_does_not_exist_and_asks_nothing(url):
    backend = _CountingBackend()
    path = URIPath(url, backend=backend)
    assert not path.bucket
    assert path.exists() is False
    assert path.is_dir() is False
    with pytest.raises(FileNotFoundError):
        path.stat()
    with pytest.raises(FileNotFoundError):
        path.read_bytes()
    with pytest.raises(FileNotFoundError):
        path.write_bytes(b"x")
    with pytest.raises(FileNotFoundError):
        list(path.iterdir())
    with pytest.raises(FileNotFoundError):
        path.mkdir()
    assert backend.asked == 0


# --- one backend for every bucket a URL names ---------------------------------------


def test_a_supplied_backend_is_the_backend_of_paths_in_different_buckets(
    moto_s3, monkeypatch
):
    import boto3

    built = []
    real = boto3.client

    def counting(*args, **kwargs):
        built.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(boto3, "client", counting)
    own = S3Backend()
    first = URIPath("s3://bkt/x", backend=own)
    second = URIPath("s3://other/y", backend=own)
    assert first.backend is own and second.backend is own
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    assert _stored(moto_s3, "x", "bkt") == b"one"
    assert _stored(moto_s3, "y", "other") == b"two"
    assert len(built) == 1
    own.close()


def test_a_supplied_backend_is_not_the_shared_one():
    own = S3Backend()
    path = URIPath("s3://bkt/a.txt", backend=own)
    assert path.backend is own
    assert path.backend is not URIPath("s3://bkt/b.txt").backend


def test_paths_without_a_backend_share_the_default_one_of_the_core_class():
    first = URIPath("s3://one/a.txt")
    second = URIPath("s3://two/b.txt")
    assert first.backend is second.backend
    assert first.backend is _s3_backend._default_backend()
    assert first.backend is S3Path("k", bucket="one").backend


def test_the_default_backend_is_not_asked_for_a_client_until_a_request(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def forbidden(*args, **kwargs):
        raise AssertionError("a path that is only built must not create a client")

    monkeypatch.setattr(boto3, "client", forbidden)
    path = URIPath("s3://bkt/dir/key") / "more"
    repr(path), str(path), hash(path), path.parent, path.bucket, path.key
    assert path.backend._client is None


# --- the store's own copy is decided from two URLs ----------------------------------


def _copy_by_the_store(source, target, sent, moto, stored_in="bkt"):
    source.copy(target)
    assert _stored(moto, "b.bin", stored_in) == b"payload"
    assert sent.count("CopyObject") == 1
    assert not {"GetObject", "PutObject"} & set(sent)


def test_copy_within_one_bucket_with_one_supplied_backend_is_one_copy_request(
    moto_s3,
):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    own = S3Backend()
    sent = _record(own)
    _copy_by_the_store(
        URIPath("s3://bkt/a.bin", backend=own),
        URIPath("s3://bkt/b.bin", backend=own),
        sent,
        moto_s3,
    )
    own.close()


def test_copy_between_two_buckets_with_one_supplied_backend_is_one_copy_request(
    moto_s3,
):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    own = S3Backend()
    sent = _record(own)
    _copy_by_the_store(
        URIPath("s3://bkt/a.bin", backend=own),
        URIPath("s3://other/b.bin", backend=own),
        sent,
        moto_s3,
        stored_in="other",
    )
    own.close()


def test_copy_between_two_buckets_on_the_default_backend_is_one_copy_request(
    moto_s3,
):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    sent = _record(URIPath("s3://bkt/a.bin").backend)
    _copy_by_the_store(
        URIPath("s3://bkt/a.bin"),
        URIPath("s3://other/b.bin"),
        sent,
        moto_s3,
        stored_in="other",
    )


@pytest.mark.parametrize("target", ["s3://bkt/b.bin", "b.bin", "/b.bin"])
def test_copy_to_a_string_in_the_same_bucket_is_one_copy_request(moto_s3, target):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    own = S3Backend()
    sent = _record(own)
    URIPath("s3://bkt/a.bin", backend=own).copy(target)
    assert _stored(moto_s3, "b.bin") == b"payload"
    assert sent.count("CopyObject") == 1
    assert not {"GetObject", "PutObject"} & set(sent)
    own.close()


def test_copy_between_two_supplied_backends_is_read_and_written(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    first, second = S3Backend(), S3Backend()
    sent_first, sent_second = _record(first), _record(second)
    URIPath("s3://bkt/a.bin", backend=first).copy(
        URIPath("s3://bkt/b.bin", backend=second)
    )
    assert _stored(moto_s3, "b.bin") == b"payload"
    assert "CopyObject" not in sent_first + sent_second
    assert "GetObject" in sent_first and "PutObject" in sent_second
    first.close()
    second.close()


def test_copy_from_a_supplied_backend_to_the_default_one_is_read_and_written(
    moto_s3,
):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    own = S3Backend()
    sent = _record(own)
    default_sent = _record(URIPath("s3://bkt/b.bin").backend)
    URIPath("s3://bkt/a.bin", backend=own).copy(URIPath("s3://bkt/b.bin"))
    assert _stored(moto_s3, "b.bin") == b"payload"
    assert "CopyObject" not in sent + default_sent
    assert "GetObject" in sent and "PutObject" in default_sent
    own.close()


def test_copy_to_a_path_of_another_class_is_read_and_written(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    own = S3Backend()
    sent = _record(own)
    target = MemPath("/out.bin")
    URIPath("s3://bkt/a.bin", backend=own).copy(target)
    assert target.read_bytes() == b"payload"
    assert "CopyObject" not in sent and "GetObject" in sent
    own.close()


def test_copy_onto_the_same_url_is_refused(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"keep")
    own = S3Backend()
    sent = _record(own)
    with pytest.raises(OSError, match="same file") as info:
        URIPath("s3://bkt/a.bin", backend=own).copy(
            URIPath("s3://bkt/a.bin", backend=own), overwrite=True
        )
    assert info.value.errno == errno.EINVAL
    assert "CopyObject" not in sent
    assert _stored(moto_s3, "a.bin") == b"keep"
    own.close()


# --- rename and move across buckets -------------------------------------------------


def test_rename_to_a_url_in_another_bucket_is_not_implemented(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    with pytest.raises(NotImplementedError):
        URIPath("s3://bkt/a.bin").rename(URIPath("s3://other/a.bin"))
    assert _keys(moto_s3) == ["a.bin"]
    assert _keys(moto_s3, "other") == []


def test_move_to_a_url_in_another_bucket_copies_and_removes_the_source(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"payload")
    URIPath("s3://bkt/a.bin").move("s3://other/dir/a.bin")
    assert _stored(moto_s3, "dir/a.bin", "other") == b"payload"
    assert _keys(moto_s3) == []


def test_move_of_a_prefix_to_another_bucket_moves_every_key(moto_s3):
    for key in ("d/a.txt", "d/sub/b.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=key.encode())
    URIPath("s3://bkt/d").move(URIPath("s3://other/moved"))
    assert [key for key in _keys(moto_s3, "other") if not key.endswith("/")] == [
        "moved/a.txt",
        "moved/sub/b.txt",
    ]
    assert [key for key in _keys(moto_s3) if not key.endswith("/")] == []


# --- pickling ----------------------------------------------------------------------


def test_a_pickled_uri_path_is_the_same_url():
    path = URIPath("s3://bkt/dir/key")
    restored = pickle.loads(pickle.dumps(path))
    assert type(restored) is S3URIPath
    assert restored == path
    assert (restored.bucket, restored.key) == ("bkt", "dir/key")
    assert restored.backend is _s3_backend._default_backend()


def test_a_pickled_uri_path_carries_no_client_option():
    own = S3Backend(region_name="eu-west-1", aws_secret_access_key="TOPSECRETVALUE")
    path = URIPath("s3://bkt/dir/key", backend=own)
    data = pickle.dumps(path)
    assert b"TOPSECRETVALUE" not in data and b"eu-west" not in data
    restored = pickle.loads(data)
    assert restored == path
    assert restored.backend is _s3_backend._default_backend()
    assert restored.backend.client_kwargs == {}


def test_a_pickled_uri_path_carries_a_backend_that_says_it_can_travel():
    path = URIPath("s3://bkt/dir/key", backend=_PicklableBackend("tag"))
    restored = pickle.loads(pickle.dumps(path))
    assert restored == path
    assert isinstance(restored.backend, _PicklableBackend)
    assert restored.backend.tag == "tag"


# --- what a callback is given ------------------------------------------------------


def test_rm_offers_the_uri_path_of_a_missing_path(moto_s3):
    offered = []
    path = URIPath("s3://bkt/missing")
    path.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append((type(error), where)) or True,
    )
    assert offered == [(FileNotFoundError, path)]
    assert type(offered[0][1]) is S3URIPath


def test_rm_offers_the_uri_path_of_each_key_the_store_refuses(moto_s3):
    for key in ("t/a", "t/b", "t/sub/c"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x")
    own = S3Backend()
    client = own.client()
    original = client._make_api_call

    def partial(name, params):
        if name != "DeleteObjects":
            return original(name, params)
        refused = {"t/b": "AccessDenied", "t/sub/c": "InternalError"}
        kept = [o for o in params["Delete"]["Objects"] if o["Key"] not in refused]
        original(name, {**params, "Delete": {"Objects": kept}})
        return {
            "Errors": [
                {"Key": key, "Code": code, "Message": "refused"}
                for key, code in refused.items()
            ]
        }

    client._make_api_call = partial
    offered = []
    URIPath("s3://bkt/t", backend=own).rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append(
            (type(error), type(where), str(where), where.bucket, where.key)
        )
        or True,
    )
    assert offered == [
        (PermissionError, S3URIPath, "s3://bkt/t/b", "bkt", "t/b"),
        (OSError, S3URIPath, "s3://bkt/t/sub/c", "bkt", "t/sub/c"),
    ]
    assert _keys(moto_s3) == ["t/b", "t/sub/c"]
    own.close()


def test_rm_offers_the_uri_path_it_was_called_on_when_a_batch_fails(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="t/a", Body=b"x")
    own = S3Backend()
    _refuse(own, "DeleteObjects", "AccessDenied")
    path = URIPath("s3://bkt/t", backend=own)
    offered = []
    path.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append((type(error), where)) or True,
    )
    assert offered == [(PermissionError, path)]
    assert type(offered[0][1]) is S3URIPath
    own.close()


def test_copy_offers_the_uri_path_of_the_child_that_failed(moto_s3):
    for key in ("d/a.txt", "d/b.txt", "d/sub/c.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=key.encode())
    own = S3Backend()
    client = own.client()
    original = client._make_api_call

    def refusing(name, params):
        if name == "CopyObject" and params["Key"] == "e/b.txt":
            raise _botoexc.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, name
            )
        return original(name, params)

    client._make_api_call = refusing
    offered = []
    URIPath("s3://bkt/d", backend=own).copy(
        URIPath("s3://bkt/e", backend=own),
        recursive=True,
        on_error=lambda error, where: offered.append(
            (type(error), type(where), str(where))
        )
        or True,
    )
    assert offered == [(PermissionError, S3URIPath, "s3://bkt/d/b.txt")]
    assert _keys(moto_s3) == [
        "d/a.txt",
        "d/b.txt",
        "d/sub/c.txt",
        "e/",
        "e/a.txt",
        "e/sub/",
        "e/sub/c.txt",
    ]
    own.close()


def test_copy_reports_progress_with_the_uri_path_of_the_source(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.bin", Body=b"x" * 10)
    own = S3Backend()
    calls = []
    source = URIPath("s3://bkt/a.bin", backend=own)
    source.copy(
        URIPath("s3://other/a.bin", backend=own),
        progress=lambda path, copied, total: calls.append((path, copied, total)),
    )
    assert calls == [(source, 10, 10)]
    assert type(calls[0][0]) is S3URIPath
    own.close()
