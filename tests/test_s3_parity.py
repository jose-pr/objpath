"""`pathlib` parity and soundness of `S3Path` and the `s3:` URI path, end to
end against moto. Every test runs on both classes. No external hosts."""

from __future__ import annotations

import errno
import gc
import io
import os
import pytest

botocore = pytest.importorskip("botocore")

from botocore.exceptions import ClientError  # noqa: E402

from objpath import BaseS3Backend  # noqa: E402
from store_support import make_path  # noqa: E402

# boto3 announces the end of its Python 3.9 support when it creates its first
# client.
pytestmark = pytest.mark.filterwarnings(r"ignore:.*Python 3\.9.*:Warning:boto3(\..*)?$")


@pytest.fixture
def moto_s3(aws_test_credentials):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        yield client


def _error(code, status, operation="Operation"):
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _Proxy:
    """The moto client with some methods replaced."""

    def __init__(self, client, **overrides):
        self._client = client
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._client, name)


class _ProxyBackend(BaseS3Backend):
    def __init__(self, proxy):
        self.proxy = proxy

    def client(self):
        return self.proxy


def _keys(client):
    return sorted(
        obj["Key"] for obj in client.list_objects_v2(Bucket="bkt").get("Contents", [])
    )


def _raise(error):
    def method(*args, **kwargs):
        raise error

    return method


def test_s3_403_is_permission_error_and_never_escapes_exists(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"a")
    denied = _raise(_error("403", 403, "HeadObject"))
    p = make_path(
        flavor,
        "s3://bkt/a.txt",
        backend=_ProxyBackend(_Proxy(moto_s3, head_object=denied)),
    )
    with pytest.raises(PermissionError):
        p.stat()
    assert p.exists() is False
    assert p.is_file() is False
    assert p.is_dir() is False


def test_s3_throttled_read_is_not_file_not_found(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"a")
    slow = _raise(_error("SlowDown", 503, "GetObject"))
    p = make_path(
        flavor,
        "s3://bkt/a.txt",
        backend=_ProxyBackend(_Proxy(moto_s3, get_object=slow)),
    )
    with pytest.raises(OSError) as info:
        p.read_bytes()
    assert type(info.value) is OSError
    assert info.value.__cause__ is None


def test_s3_missing_bucket_is_file_not_found(flavor, moto_s3):
    assert make_path(flavor, "s3://nobkt/k.txt").exists() is False
    errors = []
    assert list(make_path(flavor, "s3://nobkt/").walk(on_error=errors.append)) == []
    assert [type(e) for e in errors] == [FileNotFoundError]


def test_s3_missing_or_wrong_type_targets_raise_like_pathlib(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="file.txt", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="plain/x.txt", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="empty/", Body=b"")
    with pytest.raises(FileNotFoundError):
        list(make_path(flavor, "s3://bkt/typo").iterdir())
    with pytest.raises(NotADirectoryError):
        list(make_path(flavor, "s3://bkt/file.txt").iterdir())
    assert list(make_path(flavor, "s3://bkt/empty").iterdir()) == []
    with pytest.raises(FileNotFoundError):
        make_path(flavor, "s3://bkt/nope").rmdir()
    with pytest.raises(NotADirectoryError):
        make_path(flavor, "s3://bkt/file.txt").rmdir()
    with pytest.raises(OSError) as info:
        make_path(flavor, "s3://bkt/plain").rmdir()
    assert info.value.errno == errno.ENOTEMPTY
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt/plain").unlink()
    assert make_path(flavor, "s3://bkt/plain").is_dir()
    with pytest.raises(IsADirectoryError):
        make_path(flavor, "s3://bkt/plain").read_bytes()
    assert _keys(moto_s3) == ["empty/", "file.txt", "plain/x.txt"]


def test_s3_failed_close_upload_is_not_retried_at_gc(flavor, moto_s3):
    failures = [_error("SlowDown", 503, "PutObject")]

    def upload_fileobj(fileobj, bucket, key, *args, **kwargs):
        if failures:
            raise failures.pop()
        return moto_s3.upload_fileobj(fileobj, bucket, key, *args, **kwargs)

    backend = _ProxyBackend(_Proxy(moto_s3, upload_fileobj=upload_fileobj))
    p = make_path(flavor, "s3://bkt/state.json", backend=backend)
    f = p.open("wb")
    f.write(b'{"version": 1}')
    with pytest.raises(OSError):
        f.close()
    assert f.closed
    p.write_bytes(b'{"version": 2}')
    del f
    gc.collect()
    assert p.read_bytes() == b'{"version": 2}'


def test_s3_prefix_directory_move_falls_back_to_copy_and_delete(flavor, moto_s3):
    for key in ("plain/a.txt", "plain/sub/b.txt", "keep.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=key.encode())
    with pytest.raises(NotImplementedError):
        make_path(flavor, "s3://bkt/plain").rename("plain2")
    with pytest.raises(FileNotFoundError):
        make_path(flavor, "s3://bkt/nope").rename("nope2")
    make_path(flavor, "s3://bkt/plain").move(make_path(flavor, "s3://bkt/plain3"))
    assert _keys(moto_s3) == [
        "keep.txt",
        "plain3/",
        "plain3/a.txt",
        "plain3/sub/",
        "plain3/sub/b.txt",
    ]
    assert (
        make_path(flavor, "s3://bkt/plain3/sub/b.txt").read_bytes()
        == b"plain/sub/b.txt"
    )


def test_s3_large_write_is_multipart(flavor, moto_s3):
    import boto3
    from botocore.config import Config

    data = os.urandom(9 * 1024 * 1024)  # over boto3's 8 MiB threshold
    p = make_path(flavor, "s3://bkt/big.bin")
    with p.open("wb") as f:
        for offset in range(0, len(data), 1024 * 1024):
            f.write(data[offset : offset + 1024 * 1024])
    head = moto_s3.head_object(Bucket="bkt", Key="big.bin")
    assert head["ContentLength"] == len(data)
    assert head["ETag"].strip('"').endswith("-2")  # two parts
    # moto 5.1 reports a multipart object's composite CRC32 without its "-2"
    # suffix, so botocore's response validation rejects any read of it, plain
    # boto3 included. Real S3 and moto 5.2 mark it COMPOSITE. Compare the
    # bytes without that validation.
    reader = boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(response_checksum_validation="when_required"),
    )
    try:
        assert reader.get_object(Bucket="bkt", Key="big.bin")["Body"].read() == data
    finally:
        reader.close()


def test_s3_read_streams_the_body(flavor, moto_s3):
    data = os.urandom(1024 * 1024)
    moto_s3.put_object(Bucket="bkt", Key="big.bin", Body=data)
    sizes = []

    def get_object(**kwargs):
        response = moto_s3.get_object(**kwargs)
        body = response["Body"]

        class Recording:
            def read(self, amt=None):
                sizes.append(amt)
                return body.read(amt)

            def close(self):
                body.close()

        response["Body"] = Recording()
        return response

    streamed = make_path(
        flavor,
        "s3://bkt/big.bin",
        backend=_ProxyBackend(_Proxy(moto_s3, get_object=get_object)),
    )
    with streamed.open("rb") as f:
        assert f.read(16) == data[:16]
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"x")
    assert sizes and None not in sizes  # never one read of the whole body
    assert streamed.read_bytes() == data


def test_s3_rename_preserves_storage_class_and_uses_managed_copy_when_large(
    flavor, moto_s3, monkeypatch
):
    from objpath import _s3

    moto_s3.put_object(
        Bucket="bkt", Key="ia.txt", Body=b"ia", StorageClass="STANDARD_IA"
    )
    make_path(flavor, "s3://bkt/ia.txt").rename("ia2.txt")
    assert (
        moto_s3.head_object(Bucket="bkt", Key="ia2.txt")["StorageClass"]
        == "STANDARD_IA"
    )

    copies = []

    def copy(source, bucket, key, ExtraArgs=None, **kwargs):
        copies.append((source["Key"], key, ExtraArgs))
        return moto_s3.copy(source, bucket, key, ExtraArgs=ExtraArgs, **kwargs)

    monkeypatch.setattr(_s3, "_PUT_OBJECT_LIMIT", 1)
    backend = _ProxyBackend(_Proxy(moto_s3, copy=copy))
    make_path(flavor, "s3://bkt/ia2.txt", backend=backend).rename("ia3.txt")
    assert copies == [("ia2.txt", "ia3.txt", {"StorageClass": "STANDARD_IA"})]
    assert _keys(moto_s3) == ["ia3.txt"]
    assert moto_s3.get_object(Bucket="bkt", Key="ia3.txt")["Body"].read() == b"ia"


def test_s3_exclusive_create_is_atomic(flavor, moto_s3):
    first = make_path(flavor, "s3://bkt/locks/job.lock").open("xb")
    second = make_path(flavor, "s3://bkt/locks/job.lock").open("xb")
    first.write(b"worker1")
    second.write(b"worker2")
    first.close()
    with pytest.raises(FileExistsError):
        second.close()
    assert make_path(flavor, "s3://bkt/locks/job.lock").read_bytes() == b"worker1"
    with pytest.raises(FileExistsError):
        make_path(flavor, "s3://bkt/locks/job.lock").touch(exist_ok=False)


def test_s3_exclusive_create_without_conditional_writes_falls_back(flavor, moto_s3):
    def put_object(**kwargs):
        if "IfNoneMatch" in kwargs:
            raise _error("NotImplemented", 501, "PutObject")
        return moto_s3.put_object(**kwargs)

    backend = _ProxyBackend(_Proxy(moto_s3, put_object=put_object))
    p = make_path(flavor, "s3://bkt/new.txt", backend=backend)
    with p.open("xb") as f:
        f.write(b"created")
    assert p.read_bytes() == b"created"
    make_path(flavor, "s3://bkt/newdir", backend=backend).mkdir()
    assert "newdir/" in _keys(moto_s3)


def test_s3_rplus_writes_land(flavor, moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"hello")
    p = make_path(flavor, "s3://bkt/a.txt")
    with p.open("r+b") as f:
        assert f.read() == b"hello"
        f.seek(0)
        f.write(b"J")
    assert p.read_bytes() == b"Jello"
    with p.open("r+") as f:
        f.seek(0, io.SEEK_END)
        f.write("!")
    assert p.read_text() == "Jello!"
