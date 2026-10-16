"""`ObjectStorePath`, what `S3Path`, `GSPath` and `AZPath` share: a key
namespace with no directories, where a directory is any prefix `key/` that has
keys under it and `mkdir()` writes an empty `key/` marker."""

from __future__ import annotations

import errno as _errno
import stat as _stat
import typing as _ty
import urllib.parse as _urlparse

from pathapi import FileStat, Path, Pathname
from pathapi.providers import TreePath, check_follow, copy_error_predicate

from ._keys import (
    checked_copy,
    parse_names,
    refusal,
    split_names,
    unreachable_keys,
)
from ._places import rebuild_path

#: What a path text keeps unescaped when it is spelled as a URI.
_SAFE_KEY = "!$&'()*+,;=:@/"


class ObjectStorePath(TreePath):
    """The algorithms `S3Path`, `GSPath` and `AZPath` share. A store supplies
    the calls that differ -- one SDK request each, errors already translated
    to `OSError` (see `transport_error()`) -- and gets `stat()`, `rm()`,
    `unlink()`, `rmdir()`, `rename()`, `copy()` and the rest from here.

    A path holds the names of a key below its bucket or container, joined and
    parsed as `parse_names()` says, and the backend that reaches the store.
    Two paths are equal when they are of one class, name one bucket or
    container and spell one key; a path built from another keeps its place and
    its backend.

    Primitives a subclass defines:

    - `_stat_root()` -> `FileStat`: the bucket, container or account exists.
    - `_stat_object(key)` -> `FileStat | None`: None when no object has that
      exact key.
    - `_has_object(key)` -> bool, `_has_prefix(key)` -> bool: whether an
      object has that key, whether any key lies under `"<key>/"` (a one-item
      listing).
    - `_flat_entries(prefix)` -> `[(key, size)]`: every key under `prefix`.
    - `_first_keys(prefix, limit)` -> `[key]`: at most `limit` keys under it.
    - `_put_marker(key)`, `_delete_object(key, missing_ok=)`,
      `_delete_keys(keys, on_error)`.
    - `_rename_source(key)` -> a store-specific record, or None when there is
      no object; `_rename_copy(source, dest, dest_key)`.
    - `_stored_digests(key, algorithm)` -> `{name: hex}`, or None when there is
      no object: the digests of the whole content the store keeps.
    - `_copy_object(target, size)` -> bool: the store's own copy call, this
      object onto `target`'s key; False when the store does not implement it.
    - `_uri_root()` -> the text before the key in `as_uri()`; `_keywords()` ->
      the constructor keywords that name this path's place;
      `_default_keywords()` -> those that make its default backend.
    """

    __slots__ = ("_backend", "_place", "_hint")

    #: What the store calls its top level, for the refusals to remove it.
    _TOP = "bucket"
    #: The algorithms `supported_checksums()` offers.
    _CHECKSUMS: "frozenset[str]" = frozenset({"md5"})
    #: Exceptions `rm(recursive=True)` offers to `ignore_error` when listing a
    #: prefix fails (a missing bucket is not one).
    _LISTING_ERRORS: "tuple[type[BaseException], ...]" = (OSError,)

    if _ty.TYPE_CHECKING:
        _segments: "tuple[str, ...]"

    @staticmethod
    def _parse(path: str) -> "tuple[str, ...]":
        return parse_names(path)

    @classmethod
    def _join(cls, segments):
        # The slash that ends a piece separates it from the next one, as in
        # pathlib: `"a/"` joined with `"b"` is `a/b`.
        path = ""
        donor = None
        family = cls._family()
        for segment in segments:
            if isinstance(segment, family):
                text = segment.as_posix()
                if donor is None:
                    donor = segment
            else:
                text = "/".join(cls._text_of(segment))
            if text.startswith("/") or not path:
                path = text
            elif text:
                path = f"{path[:-1] if path.endswith('/') else path}/{text}"
        return path, donor

    @staticmethod
    def _text_of(segment) -> "tuple[str, ...]":
        if isinstance(segment, Path):
            raise NotImplementedError()
        if isinstance(segment, Pathname):
            return tuple(segment.segments)
        if isinstance(segment, str):
            return (segment,)
        raise TypeError(
            "argument should be a str or a Pathname, " f"not {type(segment).__name__!r}"
        )

    def _initialize(self, text: str, place: str, backend) -> None:
        self._segments = self._parse(text)
        self._place = place
        self._backend = backend
        self._hint = None

    def _derived(self, segments: "tuple[str, ...]"):
        """A path of this class, place and backend with the names `segments`."""
        path = object.__new__(type(self))
        path._segments = segments
        path._place = self._place
        path._backend = self._backend
        path._hint = None
        return path

    def _rebuild(self, text: str):
        return self._derived(split_names(text))

    def with_segments(self, *segments: str):
        return type(self)("/".join(segments), **self._keywords(), backend=self._backend)

    def _keywords(self) -> dict:
        """The constructor keywords that name this path's place."""
        raise NotImplementedError

    def _uri_root(self) -> str:
        raise NotImplementedError

    def is_local(self) -> bool:
        """Whether the store is this machine: never. A bucket is not a host
        name, so nothing is resolved; `PathSyncer(quick_check=True)` treats
        both sides of a sync as non-local."""
        return False

    @property
    def backend(self):
        """The backend that reaches the store for this path."""
        return self._backend

    @property
    def key(self) -> str:
        """The object key this path names: its names joined by `/`, with no
        leading `/`."""
        return "/".join(self._segments).lstrip("/")

    def _identity(self) -> tuple:
        return (self._place, self.key)

    def __eq__(self, other: object):
        if type(self) is not type(other):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash((type(self), self._identity()))

    def _order_key(self):
        return self._identity()

    def as_uri(self) -> str:
        key = self.key
        text = _urlparse.quote(key, safe=_SAFE_KEY, errors="surrogateescape")
        return f"{self._uri_root()}/{text}" + ("/" if key.endswith("/") else "")

    def __str__(self) -> str:
        return self.as_uri()

    def __reduce__(self):
        # The place and the names; a backend travels only when it says it can
        # (its options carry credentials), else the receiving process builds
        # the default one.
        keywords = self._keywords()
        if getattr(self._backend, "picklable", False):
            keywords["backend"] = self._backend
        else:
            keywords.update(self._default_keywords())
        return rebuild_path, (type(self), self._segments, keywords)

    def _default_keywords(self) -> dict:
        """The constructor keywords that make the default backend of this
        path's store, for a path that is pickled without its own backend."""
        return {}

    def _same_filesystem(self, other: "ObjectStorePath") -> bool:
        # One bucket or container reached by one backend: a client is the
        # credentials, so another backend may see another store.
        return self._place == other._place and self._backend is other._backend

    def _pop_stat_hint(self) -> "FileStat | None":
        """The stat that the listing of the parent attached to this child,
        once; the next call asks the store."""
        hint, self._hint = self._hint, None
        return hint

    def _make_child_relpath(self, name: str, stat_hint=None, **kwargs):
        """The child called exactly `name`, with the stat its listing gave."""
        segments = self._segments
        if segments == ("", ""):
            names = ("", name)
        else:
            names = (*segments, name)
        child = self._derived(names)
        child._hint = stat_hint
        return child

    def iterdir(self):
        for name, listed in self._scandir():
            yield self._make_child_relpath(name, stat_hint=listed)

    def _key_path(self, key: str) -> "ObjectStorePath":
        """The path of the object `key` in this path's bucket or container."""
        return self._derived(("", *key.split("/")) if key else ("", ""))

    def _stat_root(self) -> FileStat:
        raise NotImplementedError

    def _stat_object(self, key: str) -> "FileStat | None":
        raise NotImplementedError

    def _has_object(self, key: str) -> bool:
        raise NotImplementedError

    def _has_prefix(self, key: str) -> bool:
        raise NotImplementedError

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        raise NotImplementedError

    def _first_keys(self, prefix: str, limit: int) -> "list[str]":
        raise NotImplementedError

    def _put_marker(self, key: str) -> None:
        raise NotImplementedError

    def _delete_object(self, key: str, *, missing_ok: bool) -> None:
        raise NotImplementedError

    def _delete_keys(self, keys: "list[str]", on_error) -> None:
        raise NotImplementedError

    def _rename_source(self, key: str):
        raise NotImplementedError

    def _rename_copy(self, source, dest: "ObjectStorePath", dest_key: str) -> None:
        raise NotImplementedError

    def _stored_digests(self, key: str, algorithm: str) -> "dict[str, str] | None":
        """The digests of the whole content of the object `key` that the store
        keeps (at least `algorithm`'s, when it keeps it), by lower-case
        `hashlib` name and as hex; empty when it keeps none a reader can rely
        on. None when no object has that key."""
        raise NotImplementedError

    def _copy_object(self, target: "ObjectStorePath", size: int) -> bool:
        raise NotImplementedError

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        if key == "":
            return self._stat_root()
        found = self._stat_object(key)
        if found is not None:
            return found
        return self._stat_prefix()

    def supported_checksums(self) -> "frozenset[str]":
        """The digest algorithms the store usually keeps for an object, so a
        caller can choose one two paths share (`NativeChecksum`). It asks
        nothing of the store; `checksum()` is what decides, per object."""
        return self._CHECKSUMS

    def checksum(self, algorithm: str = "md5") -> str:
        """The hex digest of this object's whole content under `algorithm`
        (a `hashlib` name), as the store keeps it: no byte is read
        (`NativeChecksum`).

        Raises `NotImplementedError` when the store keeps no such digest of
        the content -- an ETag that is not the plain MD5 (multipart,
        KMS-encrypted), a composite checksum, an absent property -- never
        returning one that might not be. `FileNotFoundError` for a missing
        path and `IsADirectoryError` for a prefix directory; a request that
        fails raises the `OSError` it always does."""
        key = self.key
        digests = self._stored_digests(key, algorithm.lower()) if key else None
        if digests is None:
            # No object at the key: the name of a directory, or of nothing.
            if key:
                self._stat_prefix()
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        try:
            return digests[algorithm.lower()]
        except KeyError:
            raise NotImplementedError(
                f"the store keeps no {algorithm} digest of the content of {self}"
            ) from None

    def _stat_prefix(self) -> FileStat:
        """`stat()` of a key already known not to be an object -- emulate a
        directory: any object under the "<key>/" prefix means this is a
        "directory", and none means there is nothing here."""
        if self._has_prefix(self.key):
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _holds_keys(self, key: str) -> bool:
        """Whether any key lies under the prefix `key/`: a write or rename
        onto it would hide those keys behind an object. Credentials that may
        write but not list cannot ask, and are not stopped."""
        if not key:
            return False
        try:
            return self._has_prefix(key)
        except PermissionError:
            return False

    def _mkdir(self, mode):
        # stat(), not exists(): a failed probe must not read as "missing".
        try:
            self.stat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(self)
        self._put_marker(f"{self.key}/")

    def unlink(self, missing_ok=False):
        try:
            st = self.stat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if st.is_dir():
            # A prefix: deleting the bare key would delete nothing (S3) or
            # something else.
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        self._delete_object(self.key, missing_ok=missing_ok)

    def rmdir(self):
        if not self.stat().is_dir():
            raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        if not self.key:
            raise PermissionError(
                _errno.EACCES, f"removing a {self._TOP} is not supported", str(self)
            )
        marker = f"{self.key}/"
        if any(name != marker for name in self._first_keys(marker, 2)):
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        self._delete_object(marker, missing_ok=True)

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: "bool | _ty.Callable[[Exception, _ty.Self], bool]" = False,
        *,
        follow_symlinks=False,
        follow_binds=False,
        max_workers=None,
    ):
        """Remove as `Path.rm()` does. `max_workers` is `Path.rm()`'s and is
        ignored: a recursive removal is the store's batch delete."""
        # An object store holds no symlinks or bindings: the policies are
        # checked and have nothing to decide.
        check_follow("follow_symlinks", follow_symlinks)
        check_follow("follow_binds", follow_binds)
        if not recursive:
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
                follow_symlinks=follow_symlinks,
                follow_binds=follow_binds,
                # Only when given: pathapi 0.1.1 has no such keyword.
                **({} if max_workers is None else {"max_workers": max_workers}),
            )

        def on_error(error, path=None):
            if callable(ignore_error):
                return ignore_error(error, self if path is None else path)
            return bool(ignore_error)

        if not self.key:
            error = PermissionError(f"recursive {self._TOP} delete is not enabled")
            if not on_error(error):
                raise error
            return

        keys = []
        try:
            if self._has_object(self.key):
                keys.append(self.key)
        except OSError as error:
            # Not "missing": deleting the prefix tree instead of the object
            # would remove the wrong thing.
            if not on_error(error):
                raise
            return

        if not keys:
            marker = f"{self.key}/"
            try:
                entries = self._flat_entries(marker)
            except FileNotFoundError:
                # A missing bucket holds nothing: the same answer as a missing key.
                entries = []
            except self._LISTING_ERRORS as error:
                if not on_error(error):
                    raise
                return
            keys = [key for key, _size in entries]
            # What a walk of the directory would not visit is not removed
            # with it: a move copies by walking and then removes this set.
            unreachable = unreachable_keys(marker, entries)
            if unreachable:
                error = refusal(self, "remove", unreachable)
                if not on_error(error):
                    raise error
                skipped = set(unreachable)
                keys = [key for key in keys if key not in skipped]
                if not keys:
                    return

        if not keys:
            if missing_ok:
                return
            error = FileNotFoundError(self)
            if not on_error(error):
                raise error
            return

        self._delete_keys(keys, on_error)

    def _unreachable_keys(self) -> "list[str]":
        """The keys of this prefix directory that a walk does not reach, or
        `[]` for an object or a missing path."""
        key = self.key
        if key and self._has_object(key):
            return []
        prefix = f"{key}/" if key else ""
        return unreachable_keys(prefix, self._flat_entries(prefix))

    def copy(
        self,
        target,
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
        progress=None,
        on_error=None,
        max_workers=None,
    ):
        """Copy as `Path.copy()` does, with the store's own copy call for one
        object of the same store. `max_workers` is `Path.copy()`'s and applies to
        the streamed copy only; the store's copy call ignores it."""
        # A prefix is copied by walking it; one that holds keys the walk
        # cannot reach is refused up front rather than copied incompletely.
        # That refusal is not a per-child error: `on_error` is never asked.
        copy_error_predicate(ignore_error, on_error)
        if isinstance(target, str):
            target = self._coerce_target(target)
        with checked_copy(self, recursive):
            if self._store_copy_possible(target) and self._copy_in_store(
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                recursive=recursive,
                progress=progress,
            ):
                return
            return super().copy(
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=recursive,
                ignore_error=ignore_error,
                progress=progress,
                **({} if on_error is None else {"on_error": on_error}),
                # Only when given: pathapi 0.1.1 has no such keyword.
                **({} if max_workers is None else {"max_workers": max_workers}),
            )

    def _store_copy_possible(self, target) -> bool:
        """Whether one request of the store can copy this object onto
        `target`, from what the two paths say without asking: the same class,
        reached by one backend (a bucket may be another)."""
        return (
            type(target) is type(self)
            and bool(target.key)
            and self._backend is target._backend
        )

    def _copy_in_store(
        self, target, *, overwrite, follow_symlinks, recursive, progress, hint=None
    ) -> bool:
        """Copy this object onto `target` with the store's own copy call, after
        the checks `Path.copy()` makes before it writes, in the same order: the
        same file, a target that is a directory or holds keys, an existing
        target without `overwrite`. Nothing is written when one fails, and a
        copy call that fails leaves an existing target as it was.

        False when `Path.copy()` is to do it: the source is not an object (a
        prefix directory, a missing path), the store does not implement the
        call, or the source is the root of a recursive copy, which may be
        either and which `Path.copy()` asks about once, with what `move()`
        already knows. An object has no permission bits, so
        `preserve_metadata` has nothing to carry over.

        `hint` is the stat a listing gave for this path, when the caller holds
        one that this path does not."""
        hint = self._hint if hint is None else hint
        if (hint is None and recursive) or (
            hint is not None and not _stat.S_ISREG(hint.st_mode)
        ):
            return False
        try:
            src_stat = self.stat(follow_symlinks=follow_symlinks)
        except (OSError, ValueError, NotImplementedError):
            return False
        if not _stat.S_ISREG(src_stat.st_mode):
            return False
        # The same object under two spellings of its key is one file too.
        if self._same_filesystem(target) and self.key == target.key:
            raise OSError(
                _errno.EINVAL, "Source and target are the same file", str(target)
            )
        # stat(), not exists(): only FileNotFoundError means the target is
        # absent, never a failed probe.
        try:
            target_stat = target.stat()
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None:
            if _stat.S_ISDIR(target_stat.st_mode):
                raise IsADirectoryError(target)
            if not overwrite:
                raise FileExistsError(target)
        size = src_stat.st_size
        if not self._copy_object(target, size):
            return False
        if progress is not None:
            progress(self, size, size)
        return True

    def rename(self, target):
        # A plain str target is a literal sibling name; a path of another
        # bucket, another backend or another class cannot be renamed onto, and
        # `move()` copies instead.
        if isinstance(target, str):
            target = self.parent / target
        elif type(target) is not type(self) or not self._same_filesystem(target):
            raise NotImplementedError("rename() onto another bucket or backend")
        dest_key = target.key
        if dest_key == self.key:
            # Copying onto itself and then deleting the source loses the
            # object; a name that is not there is not renamed. pathlib
            # returns the new path.
            if not self._has_object(self.key):
                self._stat_prefix()
            return target
        source = self._rename_source(self.key)
        if source is None:
            # No object at the key: a prefix directory (FileNotFoundError
            # when there is nothing at all). Its keys are not renamed one by
            # one here; move() falls back to copy + rm.
            self._stat_prefix()
            raise NotImplementedError(f"rename() of the prefix directory {self}")
        if self._holds_keys(dest_key):
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(target))
        self._rename_copy(source, target, dest_key)
        return target
