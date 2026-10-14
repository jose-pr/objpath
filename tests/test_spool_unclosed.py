"""A spooled upload stream that is garbage collected unclosed uploads nothing
and warns; closing it uploads once."""

import gc
import io
import warnings

from objpath._writer import SpoolUploadStream


class _Target:
    def __init__(self):
        self.uploads = []

    def _upload_file(self, body, size, exclusive=False):
        self.uploads.append(body.read())


def _collect(make):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        make()
        gc.collect()
    return [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_an_unclosed_written_stream_is_discarded_at_collection():
    target = _Target()

    def make():
        stream = SpoolUploadStream(target)
        stream.write(b"unfinished")
        return stream

    assert len(_collect(make)) == 1
    assert target.uploads == []


def test_an_unclosed_stream_that_was_not_modified_is_collected_silently():
    target = _Target()
    assert _collect(lambda: SpoolUploadStream(target, initial=io.BytesIO(b"k"))) == []
    assert target.uploads == []


def test_a_closed_stream_uploads_once_and_collects_silently():
    target = _Target()

    def make():
        stream = SpoolUploadStream(target)
        stream.write(b"done")
        stream.close()
        return stream

    assert _collect(make) == []
    assert target.uploads == [b"done"]
