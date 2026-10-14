"""The ranged reader of an object stream."""

from __future__ import annotations

import io as _io

READ_CHUNK = 4 * 1024**2
"""Bytes of an object that one ranged request of a streamed read asks for."""


class RangeReader(_io.RawIOBase):
    """Reads an object as it is asked for, in ranged requests of `chunk` bytes,
    so a read holds one chunk and not the object, and the stream can seek.

    `fetch(start, length)` returns the bytes of the object from `start`, at
    most `length`, and nothing at or past its end; it raises the `OSError` a
    failed request means. `size()` is the object's size, asked only by
    `seek(..., SEEK_END)`. `first` is the first chunk, already fetched."""

    def __init__(self, fetch, size, first=b"", chunk=None):
        super().__init__()
        chunk = chunk or READ_CHUNK
        self._fetch, self._size_of, self._chunk_size = fetch, size, chunk
        self._start, self._pos = 0, 0
        self._view = memoryview(first)
        # A first chunk that falls short is the whole object.
        self._size = len(first) if len(first) < chunk else None

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        self._check_open()
        return self._pos

    def seek(self, offset, whence=_io.SEEK_SET):
        self._check_open()
        if whence == _io.SEEK_SET:
            pos = offset
        elif whence == _io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == _io.SEEK_END:
            if self._size is None:
                self._size = self._size_of()
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence ({whence}, should be 0, 1 or 2)")
        if pos < 0:
            raise ValueError(f"negative seek value {pos}")
        self._pos = pos
        return pos

    def readinto(self, buffer):
        self._check_open()
        if not len(buffer):
            return 0
        if not self._start <= self._pos < self._start + len(self._view):
            if self._size is not None and self._pos >= self._size:
                return 0
            # The chunk that is done with goes before the next one arrives.
            self._view = memoryview(b"")
            data = self._fetch(self._pos, self._chunk_size)
            self._start, self._view = self._pos, memoryview(data)
            if len(data) < self._chunk_size:
                self._size = self._pos + len(data)
            if not data:
                return 0
        at = self._pos - self._start
        count = min(len(buffer), len(self._view) - at)
        buffer[:count] = self._view[at : at + count]
        self._pos += count
        return count

    def _check_open(self):
        if self.closed:
            raise ValueError("I/O operation on closed file.")

    def close(self):
        self._view = memoryview(b"")
        super().close()
