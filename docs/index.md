# objpath

**`pathlib` for S3, Google Cloud Storage and Azure Blob buckets.**
`S3Path("reports/2026.csv", bucket="data")` has the method names, signatures and
exception types of `pathlib.Path`; `GSPath` and `AZPath` do the same for the other two
stores. None needs a URI, and all build on
[`pathapi`](https://jose-pr.github.io/pathapi/) alone, which holds the `Path`
base class, `glob`, `walk` and `PathSyncer`. With
[`uripath`](https://jose-pr.github.io/uripath/) installed the same package also provides
the `s3:`, `gs:` and `az:` schemes: `URIPath("s3://data/reports/2026.csv")`.

Deliberate behavioral differences from `pathlib` are listed, with their reasons, in
[Divergences from pathlib](divergences.md).

## Installation

```bash
pip install "objpath[s3]"             # or "objpath[gs]", "objpath[az]", or several
pip install "objpath[uri]"            # adds uripath and with it the s3:, gs: and az: schemes
```

No SDK is required to import the package: the first request of a store whose extra is
missing raises an `ImportError` that names the extra.

| Install | Adds |
| --- | --- |
| `pip install objpath` | the paths and the backend contracts (no SDK) |
| `pip install "objpath[s3]"` | `boto3`, for `S3Path` |
| `pip install "objpath[gs]"` | `google-cloud-storage`, for `GSPath` |
| `pip install "objpath[az]"` | `azure-storage-blob` and `azure-identity`, for `AZPath` |
| `pip install "objpath[uri]"` | `uripath` and the schemes |
| `pip install "objpath[all]"` | all of the above |

## Tour

**A path in a bucket**: the bucket is a keyword and the segments are the key; nothing
connects until the first request:

<!-- example: skip: connects to S3 -->
```python
from objpath import S3Path

report = S3Path("reports/2026.csv", bucket="data")
print(report.read_text())
for child in report.parent.iterdir():
    print(child.name, child.stat().st_size)
```

**One client for many paths**: a backend is the SDK client, and paths that share it share
its connection pool:

<!-- example: skip: connects to a store -->
```python
from objpath import S3Backend, S3Path

with S3Backend(endpoint_url="https://minio.example.org", region_name="us-east-1") as backend:
    root = S3Path(bucket="backups", backend=backend)
    (root / "2026").mkdir(exist_ok=True)
    (root / "2026" / "n.txt").write_text("1")
```

**The schemes**: with `uripath` installed, the URL of the object works too, and the same
classes do the work:

<!-- example: skip: connects to S3 -->
```python
from uripath import URIPath

print(URIPath("s3://data/reports/2026.csv").read_text())
```

See [Object-store paths](guides/paths.md) and [The schemes](guides/uri.md).
