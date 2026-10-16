# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`rm()` and `copy()` take `max_workers`**, the keyword of `pathapi`'s recursive
  `rm()` and `copy()` (a class runs serially unless it declares `_threadsafe`,
  which these classes do not). A streamed copy passes it on; the batch delete and the
  store's own copy call ignore it. Nothing changes for a call that does not give it,
  and it works with the `pathapi` release that lacks it as long as it is not given.

## [0.1.0] - 2026-10-14

The first release of `objpath`: the Amazon S3, Google Cloud Storage and Azure Blob
Storage support of `pathlib-next` 0.9.14 (`pathlib_next.uri.schemes.s3`, `.gs` and
`.az`) as a distribution of its own, with paths that need no URI. It needs Python 3.9 or
newer and `pathapi>=0.1.0,<0.2`. No store SDK is required to import it: the extras
`s3` (`boto3>=1.36.0,<2`), `gs` (`google-cloud-storage>=3.0,<4`) and `az`
(`azure-storage-blob>=12.4.0,<13` and `azure-identity>=1.0,<2`) add one, and `uri`
(`pathapi-uri>=0.1.0,<0.2`) adds the `s3:`, `gs:` and `az:` schemes. Under *Changed*, the
migration from `pathlib_next.uri.schemes` comes in this order: the table of names, the
table of extras, then every change of behaviour with what to do about it.

It is built on `pathapi`, which was published as `pathlib-next` up to 0.10.0 (import
name `pathlib_next`); the old names below are the ones before the move.

### Added

- `S3Path`, `GSPath` and `AZPath`, `pathapi.Path` classes for the objects of a
  bucket or container, built from keywords:
  `S3Path("dir/key", bucket="bkt")`, `GSPath("dir/key", bucket="bkt")`,
  `AZPath("dir/key", container="cont", account="acct")`. Nothing connects until the
  first request. Client options (`region_name=`, `project=`, `credential=`, ...) build
  the path's own backend, `backend=` shares one between paths built separately (giving it
  together with a client option is a `TypeError`), and a
  path joined from another keeps its bucket and its backend (`bucket=` or `container=`
  on a joined path moves it to another bucket of the same backend). Paths built without
  either use one default backend per process (`AZPath`: per account).
- `S3Backend`, `GSBackend`, `AZBackend` and their contracts `BaseS3Backend`,
  `BaseGSBackend` and `BaseAZBackend`. Every backend is a context manager: `close()`
  closes the SDK client it built, the next use builds another, and calling it twice is
  harmless.
- `objpath.exceptions`: `ObjPathError`, a `pathapi.PathError`, with
  `ObjectChangedError` and `UnreachableKeysError`, both also `OSError` (see *Changed*).
- `objpath.uri`: `S3URIPath`, `GSURIPath` and `AZURIPath`, the `s3:`, `gs:` and `az:`
  schemes of `uripath`, found by `uripath` through the entry points `s3`, `gs` and `az` of
  the group `uripath.schemes`. Importing `objpath.uri` without `uripath` raises an
  `ImportError` that names `pip install "objpath[uri]"`; `import objpath` never imports
  `uripath` or an SDK.
- `AZBackend.account`: the account that `account=`, else `connection_string=` or
  `account_url=`, names.
- `objpath.__version__`, read from the installed metadata.

### Changed

- The names. `pathlib_next.uri.schemes.s3`, `.gs` and `.az` are gone with no alias.

  | Was | Now |
  | --- | --- |
  | `S3Path` (a `UriPath`) | `objpath.S3Path` for a path built from keywords; `objpath.uri.S3URIPath` for `URIPath("s3://...")` |
  | `GsPath` | `objpath.GSPath`; `objpath.uri.GSURIPath` |
  | `AzPath` | `objpath.AZPath`; `objpath.uri.AZURIPath` |
  | `S3Backend`, `BaseS3Backend` | `objpath.S3Backend`, `objpath.BaseS3Backend` |
  | `GsBackend`, `BaseGsBackend` | `objpath.GSBackend`, `objpath.BaseGSBackend` |
  | `AzBackend`, `BaseAzBackend` | `objpath.AZBackend`, `objpath.BaseAZBackend` |
  | `GsPath.bucket_name` | `GSPath.bucket` (and `GSURIPath.bucket`) |
  | the entry-point group `pathlib_next.schemes`, entries `s3`, `gs`, `az` | the group `uripath.schemes`, entries `s3 = "objpath.uri:S3URIPath"`, `gs = "objpath.uri:GSURIPath"`, `az = "objpath.uri:AZURIPath"` |

- What to install where `pathlib-next` had an extra. `pathlib-next` 0.10 has no `s3`,
  `gs` or `az` extra.

  | Was | Install now |
  | --- | --- |
  | `pathlib-next[s3]` | `objpath[s3]` |
  | `pathlib-next[gs]` | `objpath[gs]` |
  | `pathlib-next[az]` | `objpath[az]` |
  | any of them, for `UriPath("s3://...")`, `gs://` or `az://` | add `objpath[uri]` (or install `uripath` beside it) |

- A path is built from keywords, not from a URL. Was: `S3Path("s3://bkt/dir/key")`
  parsed the URL, and the bucket came from its host. Now `S3Path("dir/key", bucket="bkt")`,
  `GSPath("dir/key", bucket="bkt")` and `AZPath("dir/key", container="cont",
  account="acct")`; a URL is a `URIPath` (`URIPath("s3://bkt/dir/key")`, which needs the
  `uri` extra). A path without a bucket is a `TypeError` where it is built; an
  `S3URIPath` with no bucket still raises `FileNotFoundError` when it is used.
- `str(path)` of an `S3Path`, `GSPath` and `AZPath` is the URL (`s3://bkt/dir/key`), as
  the URI paths' is. `repr()` is `S3Path('dir/key', bucket='bkt')`.
- Two paths are equal when their class, bucket (account and container, for `AZPath`)
  and key are; `x` and `/x` are one key.
- An `AZPath` needs one of `account=`, `backend=`, a client option or an `AZPath` to join
  from; without one it is a `TypeError` where it is built. Was: the account was the host of the
  URL, and a URL without one raised `FileNotFoundError` when it was used (an `AZURIPath` still
  does). For URI paths nothing else changes: `backend=` is used as it is for the path it is
  given to, whatever bucket the URI names.
- The account level of Azure is a URI feature. Was: `AzPath("az://account")` listed the
  containers of the account. Now `AZPath` always names a container; `URIPath("az://account")`
  is still a directory whose children are the containers.
- A stream whose object was replaced while it was being read raises `ObjectChangedError`,
  and a copy or removal of a prefix that holds keys a walk cannot reach raises
  `UnreachableKeysError`. Was: a plain `OSError` (`ESTALE`, `EINVAL`). Both are still
  `OSError` with the same `errno`, so `except OSError` and `errno` checks keep working.
- `AZBackend(connection_string=...)` and `AZBackend(account_url=...)` carry the account they
  name in `account`; `account` was `None` for them.
- The Azure tuning settings. Was: the module settings `schemes.az.READ_CHUNK` (512 KiB per
  ranged read request) and `schemes.az.COPY_POLL_TIMEOUT` (300 s to wait for a service copy).
  Now: the class attributes `AZBackend.read_chunk` and `AZBackend.copy_timeout`
  (`BaseAZBackend` holds the defaults); assign them on `AZBackend` or on a subclass of it.
- `is_local()` of a store path is `False` without resolving anything. Was: the URI path resolved
  the bucket or account name as if it were a host name, so `PathSyncer(quick_check=True)` made a
  DNS lookup of it and read a name that resolved to this machine as local.
- A write stream of a store that is garbage collected without being closed uploads nothing
  and emits a `ResourceWarning`; `pathlib-next` closed it, and so uploaded it. What to do:
  close the file, or use `with`.

[Unreleased]: https://github.com/jose-pr/objpath/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jose-pr/objpath/releases/tag/v0.1.0
