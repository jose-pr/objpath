"""The spooled writer of an object stream."""

from __future__ import annotations

import io as _io
import tempfile as _tempfile

from pathlib_next import providers as _providers

SPOOL_SIZE = 4 * 1024**2
"""Bytes a write stream keeps in memory before spilling to a temp file, and
the largest object a store uploads in one request."""


class SpoolUploadStream(_providers.DiscardOnError, _io.BufferedIOBase):
    """The write stream of a store that uploads an object from a file: it
    spools the writes (in memory up to `SPOOL_SIZE`, then a temp file) and
    uploads them on close() with `path._upload_file(file, size, exclusive=)`.

    `exclusive` (`open("x")`) makes the upload a conditional create. With
    `initial` (`open("r+")`) the spool starts with the object's content at
    position 0 and is uploaded only if it was modified."""

    #: Bytes kept in memory before the spool moves to a temp file.
    spool_size = SPOOL_SIZE
    _dropped = False

    def __init__(self, path, exclusive=False, initial=None):
        super().__init__()
        self._path = path
        self._exclusive = exclusive
        self._file = _tempfile.SpooledTemporaryFile(max_size=self.spool_size)
        self._dirty = initial is None
        if initial is not None:
            while True:
                chunk = initial.read(_io.DEFAULT_BUFFER_SIZE)
                if not chunk:
                    break
                self._file.write(chunk)
            self._file.seek(0)

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def _check_open(self):
        if self.closed:
            raise ValueError("I/O operation on closed file.")

    def write(self, data):
        self._check_open()
        self._dirty = True
        return self._file.write(data)

    def read(self, size=-1):
        self._check_open()
        return self._file.read(size)

    def read1(self, size=-1):
        return self.read(size)

    def seek(self, offset, whence=_io.SEEK_SET):
        self._check_open()
        self._file.seek(offset, whence)
        return self._file.tell()

    def tell(self):
        self._check_open()
        return self._file.tell()

    def truncate(self, size=None):
        self._check_open()
        self._dirty = True
        if size is None:
            size = self._file.tell()
        return self._file.truncate(size)

    def discard(self):
        """Drop the spool: `close()` uploads nothing. `open()` calls it when
        the `with` block over the stream raised."""
        self._dropped = True
        self._file.close()

    def close(self):
        if self.closed:
            return
        try:
            if self._dirty and not self._dropped:
                self._upload()
        finally:
            # Closed even when the upload fails, so `IOBase.__del__` does not
            # retry it (over newer content) at garbage collection.
            try:
                self._file.close()
            finally:
                super().close()

    def _upload(self):
        body = self._file
        body.seek(0, _io.SEEK_END)
        size = body.tell()
        body.seek(0)
        self._path._upload_file(body, size, exclusive=self._exclusive)
