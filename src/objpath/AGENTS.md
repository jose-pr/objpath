# `objpath` — public API header

Header-file-style reference for the `objpath` package: every public export with
its signature, arguments, contract and gotchas, so the package can be used
without reading its source. It ships inside the package and is
self-contained. Development documentation lives with the source at
<https://github.com/jose-pr/objpath>.

Names are imported from `objpath` (the paths, the backends, the exceptions and
`__version__`), from `objpath.exceptions` (the exceptions) and from
`objpath.uri` (the `s3:`, `gs:` and `az:` schemes of `uripath`). Every module
whose name starts with `_` is private. `import objpath` imports no store SDK and
never `uripath`: `S3Path`, `GSPath`, `AZPath` and the backends are loaded the
first time they are named, and a store's SDK is imported when its client is
built.

Install with `pip install objpath`, which needs only `pathapi`. One extra
per store brings its SDK, and a store used without its extra raises an
`ImportError` that names it:

| Extra | Adds | For |
| --- | --- | --- |
| `s3` | `boto3` | `S3Path`, `S3Backend` (and S3-compatible stores) |
| `gs` | `google-cloud-storage` | `GSPath`, `GSBackend` |
| `az` | `azure-storage-blob`, `azure-identity` | `AZPath`, `AZBackend` |
| `uri` | `uripath` | `objpath.uri`: the `s3:`, `gs:` and `az:` schemes |
| `all` | all of the above | |

`objpath.__version__` is the installed version, read from the distribution's
metadata.

## Paths (`objpath`)

```python
class S3Path(Path):
    def __init__(
        self, *segments, bucket=None, backend=None, **client_kwargs,
    ): ...
class GSPath(Path):
    def __init__(
        self, *segments, bucket=None, backend=None, timeout=..., retry=...,
        **client_kwargs,
    ): ...
class AZPath(Path):
    def __init__(
        self, *segments, container=None, account=None, backend=None,
        **client_kwargs,
    ): ...
```

`S3Path`, `GSPath` and `AZPath` are `pathapi.Path` classes (so
`pathlib.Path`'s methods, `copy()`, `move()`, `rm()`, `walk()`, `glob()` and
`PathSyncer` work on them) over an Amazon S3 bucket, a Google Cloud Storage
bucket and an Azure Blob Storage container. They need no URI. `segments` are the
object key, joined and parsed like a POSIX path; the bucket or container is a
keyword.

- **Place.** `bucket=` (`container=` for `AZPath`) names the bucket. It is
  required, unless a segment is a path of the same class and supplies it
  (`path / "x"`, `path.parent`, `S3Path(path, "x")` keep it); `bucket=` then
  moves the path to another bucket of the same backend. A path of another bucket joined as
  a later segment contributes its names only. Without a bucket: `TypeError`.
  A name that is not a `str` is a `TypeError`; an empty one or one with a `/`
  is a `ValueError`.
- **Backend.** Client options (`region_name=`, `endpoint_url=`,
  `aws_access_key_id=`, `config=`, ... for S3; `project=`, `credentials=`,
  `client_options=`, `timeout=`, `retry=`, ... for GCS; `credential=`,
  `account_url=`, `connection_string=`, ... for Azure) build the path's own
  backend (`S3Backend(**options)`), which every path derived from it keeps.
  `backend=` shares one client between paths built separately, or plugs in a
  backend of your own. Giving `backend=` together with a client option (for
  `AZPath`, also `account=`) is a `TypeError`. Without either, a path derived from
  another keeps its backend and a new path uses the default backend: one per process
  for `S3Path` and `GSPath`, one per account for `AZPath(account=...)`, shared by
  every path built that way, so they share one client and one connection pool, and
  the credentials, region and endpoint the environment gave the first request are
  theirs. `AZPath` with none of `account=`, `backend=`, a client option or a
  `AZPath` segment is a `TypeError`.
- **Nothing connects when a path is built.** A backend builds its SDK client on
  the first request.
- **Names.** `x` and `/x` are the same key (they compare equal and hash alike;
  `is_absolute()` tells them apart). Exactly one trailing `/` goes (`dir/` is
  `dir`; `dir//` is the key `dir/`), `.` segments are dropped, `..` is a name
  like any other, and an interior empty segment is part of the key (`a//b` is
  the key `a//b`, and is never listed). Leading `/`s are not part of a key.
- **Identity.** Two paths are equal when they are of one class, name one bucket
  (`AZPath`: one account and container) and spell one key; the backend is not part of
  it. They are `_same_filesystem()` when they also share a backend object.
- **Text.** `str(path)` is the URL: `s3://bucket/key`, `gs://bucket/key`,
  `az://account/container/key` (`az:///container/key` when the backend names no
  account). `as_uri()` is the same, percent-encoded. `repr()` is
  `S3Path('dir/key', bucket='bkt')`, naming `backend=` when it is not the default;
  it never shows a client option.
- **Attributes.** `path.bucket` (`S3Path`, `GSPath`), `path.container` and
  `path.account` (`AZPath`; `account` is the backend's, `None` when it names none),
  `path.key` (the object key; `""` for the root), `path.backend`.
- **Pickling and copying.** A path pickles its place and its names, and for
  `AZPath` its account, and not its backend: the options of a client carry
  credentials, so the receiving process builds the default backend. A backend
  whose class sets `picklable = True` travels with the path. `copy.copy()` and
  `copy.deepcopy()` keep the backend.

Directories do not exist in an object store, and the classes emulate them:

- `is_dir()` is prefix emulation (any key under `"<path>/"`), and `mkdir()` writes a
  zero-byte `key/` marker object; `rmdir()` needs an empty prefix; a key that is
  both an object and a prefix is the object, in `stat()` and in listings. There is
  no hierarchy: writes below a missing "directory" succeed, but a write (`"w"`) or
  `rename()` onto a prefix directory raises `IsADirectoryError` (one list request;
  credentials that may write but not list are not stopped). `mkdir()` treats only
  `FileNotFoundError` from `stat()` as absent.
- A listing is one level, with a delimiter. `_scandir()`/`iterdir()` yield each
  name with the `FileStat` the listing gave (size and `st_mtime`; no mode), so
  `is_dir()` and `stat()` of a listed child ask nothing more.
- A prefix that holds keys a directory walk does not reach (a key with an empty, `.`
  or `..` segment; the subtree under a key that is also an object; a `name/` key
  that holds data) is refused by `copy(recursive=True)`, `move()` and
  `rm(recursive=True)`: `UnreachableKeysError` naming the keys, nothing created or
  removed (`rm(..., ignore_error=...)` is offered the error and then removes only the
  keys a walk reaches). Remove the named keys, or `unlink()` the object that hides a
  subtree, first. `iterdir()` and `walk()` skip such keys.

Reading and writing:

- `open("rb")` streams the object. A read is one version of the object: the S3
  reader makes one request per open, and the GCS and Azure readers fetch ranged
  requests (4 MiB for GCS, `AZBackend.read_chunk` = 512 KiB for Azure) that each
  require the generation or ETag the first one reported, so the stream stays
  seekable and holds one chunk. An object replaced while it is being read raises
  `ObjectChangedError`, never a mix of two versions; one deleted meanwhile is
  `FileNotFoundError`. S3's stream is not seekable (`seek()` and `tell()` raise
  `io.UnsupportedOperation`).
- `open("wb")`, `"xb"` and `"r+b"` spool the writes (in memory up to 4 MiB, 8 MiB for
  S3, then a temporary file) and upload on `close()`: nothing is uploaded when the
  `with` block over the file raised, and nothing is created. `"x"` is a conditional
  create (`IfNoneMatch`, `if_generation_match=0`, `If-None-Match: *`; on S3 an upload
  above 5 GiB falls back to a check-then-put, and a store without conditional writes
  to the same check). Uploads above the spool size are multipart or chunked
  (S3 managed transfer; GCS resumable upload in 2 MiB requests; Azure blocks). `"a"`
  is unsupported (`NotImplementedError`). `st_mtime` is `LastModified`, `updated`
  or `last_modified`.
- `rename(target)`: a `str` target is a sibling. Server-side copy + delete within one
  bucket (container) reached by one backend; a target in another bucket, another
  backend or another class raises `NotImplementedError` (so `move()` copies), and so
  does a prefix directory. A missing path onto its own name is `FileNotFoundError`.
  Azure waits for the service copy at most `AZBackend.copy_timeout` (300 s),
  then aborts it and raises `TimeoutError` with the source untouched.
- `copy(target)` of a file onto a key of the same store reached by the same backend object --
  within a bucket or between buckets (Azure: within one container) -- is one request of the
  store (`CopyObject`, `copy_blob`, the service copy) and no byte passes through this process. It makes the checks of `Path.copy()` first and in
  its order (the same file, a target that is a directory or holds keys, an existing
  target without `overwrite=True`); an existing target is replaced by the copy itself,
  never deleted first. `progress` is called once with the total. A store that answers
  "not implemented" gets the streamed copy, and a copy between different backends or
  classes always streams. `copy(recursive=True)` and `move()` of a prefix copy each file
  this way; a file named as the root of a `recursive=True` copy is read and written.
- `rm(recursive=True)` batch-deletes (1000 keys per request on S3, 100 on GCS, 256 on
  Azure, each key on its own where the batch is refused); at the bucket root it is
  `PermissionError`. A key a batch refuses is offered to `ignore_error(error, path)`
  with that key's path, one error per key. `rm(recursive=True)` of a missing bucket is
  a missing path (`missing_ok`, `ignore_error` decide). There are no symlinks or
  bindings: `follow_symlinks=` and `follow_binds=` are checked and decide nothing.
- `checksum(algorithm="md5")` (`NativeChecksum`) is the digest the store keeps, read with
  one request and no byte of the content. S3: the plain MD5 that the ETag is for an object
  one request wrote and KMS or a customer key does not encrypt, or the stored full-object
  `crc32`, `crc32c`, `crc64nvme`, `sha1` or `sha256`. GCS: `md5` or `crc32c`. Azure: the
  `Content-MD5` property, which a client can set. A multipart ETag, a composite checksum,
  an algorithm the object has none of, a content-encoded GCS object: `NotImplementedError`,
  never a guess. `FileNotFoundError` for a missing path, `IsADirectoryError` for a prefix
  directory. `supported_checksums()` is `{"md5"}` (`{"md5", "crc32c"}` for GCS), asks
  nothing and is advisory. `PathSyncer` compares two object-store paths by these digests.
- There is no `chmod()`, `symlink_to()` or `readlink()`.
- `is_local()` is `False`: a bucket is not a host name, nothing is resolved, and
  `PathSyncer(quick_check=True)` treats a store as non-local.
- Timeouts and retries are the SDK's own: pass `config=botocore.config.Config(...)` (S3),
  `timeout=` / `retry=` (GCS, on `GSBackend`) or `retry_*`, `connection_timeout=`,
  `read_timeout=` (Azure) to change them. With the SDK defaults a connection and a
  read may each wait a minute or more.

## Backends (`objpath`)

```python
class BaseS3Backend:
    def client(self): ...
    def close(self) -> None: ...
    picklable: bool
class S3Backend(BaseS3Backend):
    client_kwargs: dict
    def __init__(self, **client_kwargs): ...

class BaseGSBackend:
    def client(self): ...
    def call_options(self) -> dict: ...
    def close(self) -> None: ...
class GSBackend(BaseGSBackend):
    client_kwargs: dict
    def __init__(self, *, timeout=..., retry=..., **client_kwargs): ...

class BaseAZBackend:
    account: str | None
    read_chunk: int
    copy_timeout: float
    def client(self): ...
    def close(self) -> None: ...
class AZBackend(BaseAZBackend):
    client_kwargs: dict
    def __init__(self, account=None, **client_kwargs): ...
```

A backend gives the paths that hold it their SDK client. Subclass the `Base...Backend`
of a store and define `client()` to plug in your own credential or session handling (or a
fake in a test); the real classes build the client on first use, once however many threads
ask first, and keep it. A client is shared between threads (the SDKs document that).

- `S3Backend(**client_kwargs)` is `boto3.client("s3", **client_kwargs)`; boto3's own
  credential and endpoint configuration by default. `BaseS3Backend.client()` returns
  the boto3 (or botocore) S3 client.
- `GSBackend(*, timeout=, retry=, **client_kwargs)` is `google.cloud.storage.Client(
  **client_kwargs)` (an emulator: `client_options={"api_endpoint": url},
  use_auth_w_custom_endpoint=False`, or set `STORAGE_EMULATOR_HOST`). `timeout` (seconds
  or `(connect, read)`) and `retry` (`google.api_core.retry.Retry`, or `None` for none)
  are passed to every SDK call a path makes (`call_options()` is the hook); left out, the
  SDK's defaults apply, under which an unreachable endpoint fails after about two minutes.
- `AZBackend(account=None, **client_kwargs)`: `connection_string=` goes to
  `BlobServiceClient.from_connection_string(...)`; otherwise the options go to
  `BlobServiceClient`, and `account` alone derives
  `account_url="https://<account>.blob.core.windows.net"` authenticated by
  azure-identity's `DefaultAzureCredential` unless `credential=` is passed (`ImportError`
  naming `objpath[az]` if azure-identity is missing). `backend.account` is `account`, else the
  account `connection_string=` (`AccountName`) or `account_url=` names, else `None`.
- `read_chunk` (bytes of a blob one ranged read request asks for, default 524288; the SDK holds
  about nine times a response while it handles it, so a larger number trades memory for requests) and
  `copy_timeout` (seconds, default 300.0) are class attributes of `BaseAZBackend`: assign them on
  `AZBackend` or on your own subclass.
- Every backend is a context manager. `close()` closes the SDK client the backend built
  (when the SDK's client has a `close()`) and drops it: the next `client()` builds
  another. It is complete when it returns and harmless when called again.
- A backend pickles and copies (`copy.copy`, `deepcopy`) without the client it built and
  keeps its options, credentials included: the copy builds its own client on first use.
  A path never pickles its backend unless the backend's `picklable` is true (it is
  `False`).

## The `s3:`, `gs:` and `az:` schemes (`objpath.uri`, `uri` extra)

```python
class S3URIPath(DelegatingURIPath): bucket: str; key: str
class GSURIPath(DelegatingURIPath): bucket: str; key: str
class AZURIPath(DelegatingURIPath): account: str; container: str; key: str
```

Importing `objpath.uri` needs `uripath` (`ImportError` naming `pip install
"objpath[uri]"` without it) and imports no SDK. `uripath` loads the module by itself,
through the entry points of the group `uripath.schemes`, the first time a path of one of the
schemes is used: `URIPath("s3://bucket/key")` is an `S3URIPath`. Each class is a
`uripath` URI path (see its header for everything about URIs) whose requests are done by
the matching path of this package, on `backend`: a `BaseS3Backend`, `BaseGSBackend` or
`BaseAZBackend` used as it is by the path it is given to and by the paths derived from it
on the same bucket; without one, the
default backend of the process (`s3:`, `gs:`) or of the account in the authority (`az:`).

- `s3://bucket/key`, `gs://bucket/key`: `bucket` is the host. `az://account/container/key`:
  `account` is the host and `container` the first segment; `az://account` is a directory
  whose children are the containers, and reading or writing it is `IsADirectoryError`.
- The key is the path without its leading `/`s and without one trailing `/`; an interior
  empty segment is part of it (`s3://b/a//b` is the key `a//b`). A key that begins with
  `/` cannot be addressed.
- A path with no bucket (`s3:///key`) or no account (`az:x`) does not exist:
  `FileNotFoundError`, nothing is sent and no credential is built.
- `rename()` and server-side `copy()` between two URIs need the same authority (and, for
  `az:`, container) and the same supplied backend (or each its default); otherwise
  `rename()` raises `NotImplementedError` and `move()` copies. Callbacks of `rm()` and
  `copy()` (`ignore_error`, `on_error`, `progress`) are given URI paths.
- A URI path pickles as its URI text (no password: the URI holds none) and not its
  backend, as a path does.

## Exceptions (`objpath.exceptions`)

```python
class ObjPathError(pathapi.PathError): ...
class ObjectChangedError(ObjPathError, OSError): ...
class UnreachableKeysError(ObjPathError, OSError): ...
```

A failure of a store is the `OSError` type `pathlib` raises for the same condition, with
the path as `filename`: `FileNotFoundError` (a key or a bucket), `PermissionError`,
`OSError(EIO)`; a request that runs out of time is `TimeoutError`, an endpoint that cannot
be reached `ConnectionError`, a connection that breaks while the answer or a body arrives
`ConnectionResetError`. None is chained to the SDK's exception, and the message carries the
SDK's exception type but not its text (it holds the request URL and so any query-string
credential). They reach `exists()` and `walk(on_error=)` as `OSError`. An exception that is
not a reply or a connection failure (a missing SDK, bad credentials, a bug) propagates as
itself.

- `ObjectChangedError` (`errno.ESTALE`, `EIO` where the platform has none): the object a
  stream was reading was replaced.
- `UnreachableKeysError` (`errno.EINVAL`): a copy or removal of a prefix was refused because
  it holds keys a listing does not show; nothing changed.
- A bad argument is a plain `ValueError` or `TypeError`.

## Gotchas

- A key that begins with `/` is not addressable (`S3Path("//lead.txt")` is the key `lead.txt`)
  and a key with an empty segment (`a//b`) is addressable but never listed.
- `import objpath` and `objpath.uri` load no SDK; naming `objpath.S3Path` imports
  `botocore.exceptions` and `boto3.exceptions` when they are installed, to know which errors to
  translate.
- A test double that stands for a client subclasses the `Base...Backend` of the store and
  defines `client()`; a `Mock` client needs the methods a path calls (`head_object`,
  `list_objects_v2`, ...), whose names are the SDK's.
- Listing a very large prefix is one request per page; `rm(recursive=True)` and a recursive copy
  list the whole prefix first.
