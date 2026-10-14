"""Builds the path a test runs on, of the class its `flavor` names, from one URL:
`make_path(flavor, "s3://bkt/dir/key", backend=...)` is an `S3Path` for
`"path"` and the `S3URIPath` of the same URL for `"uri"`."""

from __future__ import annotations

from urllib.parse import unquote


def split_url(url: str) -> "tuple[str, list[str]]":
    """`(scheme, [authority, path...])`: `s3://bkt/a/b` is `("s3", ["bkt", "a/b"])`
    and `az://acct/cont/a/b` is `("az", ["acct", "cont", "a/b"])`. The key text
    keeps its slashes, an empty segment and a trailing slash among them."""
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise ValueError(f"not an object-store URL: {url!r}")
    authority, _, path = rest.partition("/")
    if scheme == "az":
        container, _, key = path.partition("/")
        return scheme, [authority, container, key]
    return scheme, [authority, path]


def make_path(flavor: str, url: str, *, backend=None, **options):
    """The path `url` names, of the class `flavor` selects (`"path"` or `"uri"`).
    `backend` is passed as `backend=`; `options` are the client options of the
    core class (`"path"` only) and the keywords of `URIPath` (`"uri"`)."""
    if flavor == "uri":
        from uripath import URIPath

        if backend is not None:
            options["backend"] = backend
        return URIPath(url, **options)
    scheme, parts = split_url(url)
    key = unquote(parts[-1])
    if backend is not None:
        options["backend"] = backend
    if scheme == "s3":
        from objpath import S3Path

        return S3Path(key, bucket=parts[0], **options)
    if scheme == "gs":
        from objpath import GSPath

        return GSPath(key, bucket=parts[0], **options)
    if scheme == "az":
        from objpath import AZPath

        if backend is None and "account" not in options:
            options["account"] = parts[0]
        return AZPath(key, container=parts[1], **options)
    raise ValueError(f"not an object-store URL: {url!r}")


def uri_of(path) -> str:
    """The text of `path` as the URL it names: the same for both flavors."""
    return str(path)
