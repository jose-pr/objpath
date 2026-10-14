# Divergences from `pathlib`

`objpath` targets `pathlib.Path` parity: same method names, signatures, semantics and
exception types wherever a `pathlib.Path` equivalent exists. Extensions (extra optional
kwargs, new methods like `rm()` and `copy()`) are allowed. Any *behavioral* divergence from
`pathlib` on a method that exists in both must be listed here -- no silent divergence.

The divergences below apply to `S3Path`, `GSPath`, `AZPath` and the URI paths that delegate
to them. The ones of the generic `Path` operations that every path inherits (`touch()` on a
non-local backend, `rm()` and its policies, `chown()`, `Path.replace()`, a `with` block that
raised while writing) are on the documentation site of `pathapi`,
<https://jose-pr.github.io/pathapi/divergences/>.

| Method | `pathlib` | `S3Path`, `GSPath`, `AZPath` | Why |
| --- | --- | --- | --- |
| Directories (`is_dir()`, `mkdir()`, `rmdir()`) | directories are real file system entries | `is_dir()` is prefix emulation (any key under `"<path>/"`); `mkdir()` creates a zero-byte `"<path>/"` marker object; `rmdir()` requires no other keys under that prefix (pathlib-parity "must be empty"). If an exact object key and a `"<path>/"` prefix both exist, exact-object operations such as `stat()` and `rm(recursive=True)` treat the path as the object first. | An object store has no directory concept; the marker is the prefix convention the AWS console itself uses for an empty "folder". Exact-object precedence avoids deleting a prefix tree when the addressed path is a real object. |
| A relative path, and the empty path | relative to the current working directory | names are read from the bucket root: `S3Path("x", bucket=b)` is the key `x`, `x` and `/x` are one key, and the empty path is the bucket root | A bucket has no working directory, and two spellings of one name must be one object (`==`, `copy()` and `move()` ask). |
| An empty segment in a path (`S3Path("a//b", bucket=b)`), a trailing `/` | `PurePosixPath("a//b")` is `a/b`: pathlib collapses empty segments and drops trailing slashes | The empty segment is kept: the key is `a//b`. Exactly one trailing `/` is dropped (`dir/` is `dir`), so `dir//` is the key `dir/`; `.` segments are dropped. A key that begins with `/` cannot be addressed. | On an object store `d//y.txt` and `d/y.txt` are two keys, so a key that holds one could not be addressed otherwise. |
| `..` in a path | applied by `resolve()` and kept by `PurePath` | a name like any other: `S3Path("a/../b")` is the key `a/../b`. `iterdir()` and `walk()` never yield such a key. | A store resolves nothing; the key is what it is. |
| `==` and `hash()` | equal when the path text is | equal when the class, the bucket (the account and container, for `AZPath`) and the key are; the backend is not part of it | Two buckets with one key are two objects. |
| `str(path)` | the file path | the URL: `s3://bucket/key`, `gs://bucket/key`, `az://account/container/key` | The text has to say which bucket the key is in. |
| `rename(target)` | POSIX `rename(2)` | Server-side copy + delete, within one bucket (container) reached by one backend. A `str` target is a sibling name. A target elsewhere, and a prefix directory, raise `NotImplementedError`, so `move()` copies and removes. A missing source onto its own name is `FileNotFoundError`. | There is no atomic rename, and no prefix rename. |
| `open("x")` | exclusive create is atomic | Atomic through a conditional put (`IfNoneMatch`, `if_generation_match=0`, `If-None-Match: *`), except an S3 upload above 5 GiB (multipart uploads accept no `IfNoneMatch`) or to a store without conditional writes, which check and then put. | The store has no other primitive. |
| `open("a")`, `open("r+")` | append and update in place | `"a"` raises `NotImplementedError`; `"r+"` downloads the object, spools the writes and uploads the whole object on `close()` | An object is replaced as a whole. |
| `chmod()`, `chown()`, `symlink_to()`, `hardlink_to()`, `readlink()` | supported where the platform does | not supported | An object has no mode, owner or links. |
| `stat()` | a full `os.stat_result` | `FileStat` with the size and `st_mtime` (the store's last-modified time) of an object; a directory has neither; the mode is a placeholder | The listing carries no more. |
| `utime()`, `touch()` of an existing object | sets the time | not settable | The store sets the time. |
| `iterdir()` of a prefix that holds keys a walk cannot reach (an empty or `..` segment, the subtree under a key that is also an object) | lists what the directory holds | skips them; `copy(recursive=True)`, `move()` and `rm(recursive=True)` of the prefix refuse with `UnreachableKeysError`, naming the keys, and change nothing | A copy or removal that left the unreachable keys behind would be half done. |
| Reading while the object changes | not applicable | a read is one version of the object: the stream raises `ObjectChangedError` if it is replaced meanwhile | Two versions of one object are never one file. |
| `checksum()` | not a `pathlib` method | the digest the store keeps, no byte read; `NotImplementedError` where the store keeps none that is the content's (a multipart ETag, a composite checksum) | Never a guess. |
