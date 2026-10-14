"""`ObjectStoreURIPath`, what `S3URIPath`, `GSURIPath` and `AZURIPath` share: a
URI path that hands every request to the path of its store that needs no URI,
and keeps what is about URIs -- the bucket or container the authority and the
first segment name, the backend the paths of one process share, and the
paths a callback is given."""

from __future__ import annotations

import typing as _ty

from pathapi.providers import check_follow, copy_error_predicate
from uripath.schemes import DelegatingURIPath

from ._keys import parse_names
from ._keys import checked_copy


class ObjectStoreURIPath(DelegatingURIPath):
    """A URI path of an object store, done by the path of the store that
    needs no URI (`S3Path`, `GSPath`, `AZPath`) on the backend this path
    carries. Subclasses say which bucket or container a URI names
    (`_make_core()`) and how a path of the store maps back to a URI
    (`_from_core()`)."""

    __slots__ = ()
    _DELEGATED = ("_scandir", "_listdir", "_open", "_mkdir", "unlink", "rmdir")

    def _key_text(self, uri) -> str:
        """The part of the path of `uri` that is the object key, rooted: the
        path itself, or what follows the container."""
        return uri.path or "/"

    @property
    def key(self) -> str:
        """The object key this path names: the path without its leading `/`s
        and without one trailing `/`; an interior empty segment is part of
        the key."""
        return "/".join(parse_names(self._key_text(self))).lstrip("/")

    def _core_of(self, target) -> "ObjectStoreURIPath":
        """The path of this path's core backend that the URI `target` names."""
        return self._core_path.with_segments(self._key_text(target))

    def _from_core(self, path) -> "ObjectStoreURIPath":
        """The URI path on this endpoint for a path of the core backend."""
        raise NotImplementedError

    def _store_copy_possible(self, target) -> bool:
        """Whether one request of the store can copy this object onto `target`,
        from what the two URIs say without asking: the same class, reached by
        one client (one supplied backend, or each path's own default)."""
        return (
            type(target) is type(self)
            and bool(target.key)
            and self._supplied_backend() is target._supplied_backend()
        )

    def is_local(self) -> bool:
        """Whether the store is this machine: never. The host of the URI is a
        bucket or an account, not a name to resolve."""
        return False

    def supported_checksums(self) -> "_ty.FrozenSet[str]":
        with self._delegated():
            return self._core_path.supported_checksums()

    def checksum(self, algorithm: str = "md5") -> str:
        with self._delegated():
            return self._core_path.checksum(algorithm)

    def rename(self, target):
        # A plain str target is a sibling rename (relative to self's parent): a
        # literal name, not re-parsed as a URI. A target in another bucket or
        # container cannot be reached by a native rename (NotImplementedError,
        # so `move()` copies).
        target = self._rename_target(target)
        if isinstance(target, ObjectStoreURIPath) and not self._same_filesystem(target):
            raise NotImplementedError("rename() onto another backend")
        destination = self.with_path(target.path)
        moved = self._core_of(target)
        with self._delegated((str(moved), str(destination))):
            self._core_path.rename(moved)
        # pathlib returns the new path.
        return destination

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: "bool | _ty.Callable[[Exception, _ty.Self], bool]" = False,
        *,
        follow_symlinks=False,
        follow_binds=False,
    ):
        check_follow("follow_symlinks", follow_symlinks)
        check_follow("follow_binds", follow_binds)
        on_error = ignore_error
        if callable(ignore_error):

            def on_error(error, path=None):
                return ignore_error(
                    error, self if path is None else self._from_core(path)
                )

        with self._delegated():
            return self._core_path.rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=on_error,
                follow_symlinks=follow_symlinks,
                follow_binds=follow_binds,
            )

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
    ):
        # The core path's walk of a prefix is not used: children are URI paths
        # and are copied through this method, so the refusal of a prefix that
        # holds keys a walk cannot reach is made here, once.
        offering = copy_error_predicate(ignore_error, on_error)
        if isinstance(target, str):
            target = self._coerce_target(target)
        with checked_copy(self._core_path, recursive):
            if self._store_copy_possible(target):
                shown = progress
                if progress is not None:

                    def shown(_source, done, total):
                        progress(self, done, total)

                moved = target._core_path
                with self._delegated((str(moved), str(target))):
                    done = self._core_path._copy_in_store(
                        moved,
                        overwrite=overwrite,
                        follow_symlinks=follow_symlinks,
                        recursive=recursive,
                        progress=shown,
                        hint=self._stat_hint,
                    )
                if done:
                    return
            return super().copy(
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=recursive,
                ignore_error=ignore_error,
                progress=progress,
                **({} if offering is None else {"on_error": offering}),
            )
