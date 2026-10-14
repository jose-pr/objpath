# objpath

[![Version](https://img.shields.io/pypi/v/objpath.svg)](https://pypi.org/project/objpath/)
[![Python versions](https://img.shields.io/pypi/pyversions/objpath.svg)](https://pypi.org/project/objpath/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/jose-pr/objpath/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/objpath/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/objpath/test.yml)](https://github.com/jose-pr/objpath/actions/workflows/test.yml)

**`pathlib` for S3, Google Cloud Storage and Azure Blob buckets.**
`S3Path("reports/2026.csv", bucket="data")` has the method names, signatures and
exception types of `pathlib.Path`, and `GSPath` and `AZPath` do the same for the
other two stores; none needs a URI. They build on
[`pathapi`](https://github.com/jose-pr/pathapi) alone; with
[`uripath`](https://github.com/jose-pr/uripath) installed the same package also
provides the `s3:`, `gs:` and `az:` schemes. It is documented at
<https://jose-pr.github.io/objpath/>.

## Features

| Store | Class | Extra | Directories | Server-side copy | Checksum | Append |
| --- | --- | --- | --- | --- | --- | --- |
| Amazon S3 and S3-compatible | `S3Path` | `s3` | key prefixes, `mkdir()` writes a marker object | `CopyObject`, same client | ETag MD5, `crc32`, `crc32c`, `crc64nvme`, `sha1`, `sha256` | no |
| Google Cloud Storage | `GSPath` | `gs` | key prefixes, `mkdir()` writes a marker object | `copy_blob`, same client | `md5`, `crc32c` | no |
| Azure Blob Storage | `AZPath` | `az` | key prefixes, `mkdir()` writes a marker blob | service copy, same container and client | `Content-MD5` | no |

- **A `pathlib` path on a bucket**: `open`, `read_text`, `iterdir`, `glob`, `stat`,
  `mkdir`, `rename` and the rest, plus `walk`, `copy`, `move`, `rm` and `PathSyncer` from
  `pathapi`. Nothing connects until the first request.
- **Directories without directories**: `is_dir()` is a prefix test, listings use a
  delimiter and carry the size and time of each object, and the keys a walk cannot reach
  (an empty segment, a name that is both an object and a prefix) are named and refused
  instead of copied or removed half way.
- **Batch deletes and server-side copies**: `rm(recursive=True)` deletes a thousand keys a
  request, and a copy within a store moves no byte through your process.
- **Safe writes**: uploads are spooled and sent on `close()`; a `with` block that raised
  uploads nothing. A read is one version of the object, and an object replaced meanwhile
  is an error, not a mix.
- **The SDKs stay out of the way**: `import objpath` loads no SDK and not `uripath`; a
  store used without its extra raises an `ImportError` naming it. No SDK exception crosses
  the API: errors are the `OSError` types `pathlib` raises, `TimeoutError` and
  `ConnectionError`.
- **The `s3:`, `gs:` and `az:` schemes** for `uripath`: `URIPath("s3://data/reports")` is
  an `S3URIPath` that delegates to an `S3Path`.

## Installation

```bash
pip install "objpath[s3]"
```

Requires `pathapi`, which pip installs with it, and Python 3.9 or newer.

| Install | Adds |
| --- | --- |
| `pip install objpath` | the paths and the backend contracts; no SDK |
| `pip install "objpath[s3]"` | `boto3>=1.36.0,<2` for `S3Path` |
| `pip install "objpath[gs]"` | `google-cloud-storage>=3.0,<4` for `GSPath` |
| `pip install "objpath[az]"` | `azure-storage-blob` and `azure-identity` for `AZPath` |
| `pip install "objpath[uri]"` | `uripath` and with it the `s3:`, `gs:` and `az:` schemes |
| `pip install "objpath[all]"` | all of the above |

## Quick start

**A path in a bucket**: the bucket is a keyword, the segments are the key, and the
credentials are the ones boto3 finds:

<!-- example: skip: connects to S3 -->
```python
from objpath import S3Path

report = S3Path("reports/2026.csv", bucket="data")
print(report.read_text())
for child in report.parent.iterdir():
    print(child.name, child.stat().st_size)
```

**Another store, or another endpoint**: client options build the path's own backend, and a
backend shared between paths is one client and one connection pool:

<!-- example: skip: connects to a store -->
```python
from objpath import AZPath, GSPath, S3Backend, S3Path

blob = AZPath("logs/app.log", container="ops", account="mycompany")
gcs = GSPath("models/latest", bucket="ml", project="my-project")

with S3Backend(endpoint_url="https://minio.example.org", region_name="us-east-1") as backend:
    root = S3Path(bucket="backups", backend=backend)
    (root / "2026").mkdir(exist_ok=True)
    (root / "2026" / "n.txt").write_text("1")
    root.copy(S3Path("mirror", bucket="backups", backend=backend), recursive=True)
```

**Mirror a tree** with `PathSyncer`, onto a local directory:

<!-- example: skip: connects to S3 -->
```python
from pathapi import LocalPath
from pathapi.sync import PathSyncer
from objpath import S3Path

PathSyncer(remove_missing=True).sync(S3Path("logs", bucket="data"), LocalPath("mirror"))
```

**The schemes**, with `uripath` installed:

<!-- example: skip: connects to S3 -->
```python
from uripath import URIPath

print(URIPath("s3://data/reports/2026.csv").read_text())
```

## API overview

| Module | Purpose |
| --- | --- |
| `objpath` | `S3Path`, `GSPath`, `AZPath`, the backends `S3Backend`, `GSBackend`, `AZBackend` and the contracts `BaseS3Backend`, `BaseGSBackend`, `BaseAZBackend`, the exceptions and `__version__` |
| `objpath.exceptions` | `ObjPathError`, `ObjectChangedError` and `UnreachableKeysError` |
| `objpath.uri` | `S3URIPath`, `GSURIPath` and `AZURIPath`, the schemes of `uripath` (needs the `uri` extra) |

The full reference is on the [documentation site](https://jose-pr.github.io/objpath/);
an agent-oriented API summary ships inside the package as `objpath/AGENTS.md`.

## Development

```bash
pip install -e ".[dev,all]"
python -m pytest -q -rs
```

The S3 tests run against `moto` in the test process, and the Google Cloud Storage and
Azure tests against fake servers on the loopback interface; none reaches another host. A
test for an extra that is not installed is skipped, so check the skip list (`-rs`).
Contributor notes (virtual environments, formatting, CI, releases) are in the repository's
[`AGENTS.md`](https://github.com/jose-pr/objpath/blob/main/AGENTS.md). To preview the
documentation: `pip install -e ".[all,docs]"` then `mkdocs serve`.

Changes are recorded in the
[changelog](https://github.com/jose-pr/objpath/blob/main/CHANGELOG.md).

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](https://github.com/jose-pr/objpath/blob/main/CHANGELOG.md). Pushing a tag matching `v*` triggers the release
workflow: test gate → build (checking the tag names the version built) → a strict
docs build as a gate → GitHub release → publish. The release workflow never deploys
the docs site itself: for a final release its last job dispatches the docs workflow
at the tag, which owns every Pages deploy.

## License

MIT — see [LICENSE](https://github.com/jose-pr/objpath/blob/main/LICENSE).
