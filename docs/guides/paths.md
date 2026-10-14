# Object-store paths

`S3Path`, `GSPath` and `AZPath` are `pathapi.Path` classes for the objects of an
Amazon S3 bucket, a Google Cloud Storage bucket and an Azure Blob Storage container. They
support what `pathlib.Path` does on a file system (`open`, `read_text`, `write_bytes`,
`iterdir`, `glob`, `stat`, `mkdir`, `unlink`, `rmdir`, `rename`, ...) and what
`pathapi` adds (`walk`, `copy`, `move`, `rm`, `PathSyncer`).

## Naming the bucket

The bucket (the container, for Azure) is a keyword and the segments are the key:

```python
from objpath import AZPath, GSPath, S3Path

S3Path("reports/2026.csv", bucket="data")
GSPath("models", "latest", bucket="ml")                      # the key models/latest
AZPath("logs/app.log", container="ops", account="mycompany")
```

A path joined from another keeps its bucket and its backend, and so do `parent`,
`with_name`, `iterdir` and `relative_to`; `bucket=` on a joined path moves it to another
bucket of the same backend. `reports/2026.csv` and `/reports/2026.csv` are one key. A
trailing `/` is dropped, and an empty segment inside a key (`a//b`) is part of it.

## Backends and credentials

A backend is the SDK client; paths that hold the same backend share its connection pool.
Client options build the path's own backend, `backend=` shares one, and a path with
neither uses the default backend of the process (`AZPath`: of the account):

```python
from objpath import AZBackend, S3Backend, S3Path

S3Path("x", bucket="b", region_name="eu-west-1")                 # its own S3Backend
backend = S3Backend(endpoint_url="http://127.0.0.1:9000")        # MinIO, say
S3Path("x", bucket="b", backend=backend)                         # a shared one
AZBackend(connection_string="...")                               # names its account itself
```

Giving `backend=` together with a client option is a `TypeError`. Credentials are whatever
the SDK finds, or the options you pass; a pickled path carries none of them, so the
receiving process builds the default backend. A backend is a context manager: leaving the
block closes the SDK client, and a later use builds another.

Timeouts and retries are the SDK's own: botocore's `config=Config(...)`, `timeout=` and
`retry=` on `GSBackend`, and the `retry_*`, `connection_timeout=` and `read_timeout=`
options of the Azure client.

## Directories

An object store has none. `is_dir()` is true when some key starts with `"<path>/"`,
`mkdir()` writes a zero-byte `"<path>/"` marker, and `rmdir()` needs an empty prefix. A
listing is one level at a time with a delimiter and carries each object's size and time,
so `is_dir()` and `stat()` of a listed child cost nothing more.

Some keys cannot be reached by a walk: one with an empty or `.`/`..` segment, the subtree
under a key that is also an object. `copy(recursive=True)`, `move()` and
`rm(recursive=True)` of a prefix that holds one raise `UnreachableKeysError` before they
change anything, naming the keys.

## Reading and writing

A read streams one version of the object; an object replaced while it is being read
raises `ObjectChangedError`. A write spools in memory (then a temporary file) and uploads
on `close()`; if the `with` block raised, nothing is uploaded. `open("x")` is a
conditional create. A file `copy()` within one store is one request of the store, and
`rm(recursive=True)` deletes in batches.

## Errors

A failed request is the `OSError` pathlib raises for it, with the path as `filename`: a
missing key or bucket is `FileNotFoundError`, a refusal `PermissionError`, a request that
ran out of time `TimeoutError`, an unreachable endpoint `ConnectionError`. Nothing of the
SDKs crosses the API, and the message carries the SDK's exception type but not its text
(which holds the request URL).
