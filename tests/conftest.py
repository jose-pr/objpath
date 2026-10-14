import base64
import contextlib
import hashlib
import http.server
import sys
import tempfile
import threading

import pytest

import hermetic
import objstore_fakes
from hermetic import NetworkAccessBlocked  # noqa: F401  (re-exported for tests)


@pytest.fixture
def fixture_tree(tmp_path):
    """A small fixture tree for glob/walk/contract tests:

    root/
      a.txt
      b.py
      .hidden.txt
      sub/
        c.py
        nested/
          d.py
      empty_dir/

    Built by the shipped `pathapi.testing.populate_fixture_tree()`, so
    the in-repo contracts and downstream users share one definition.
    """
    from pathapi.testing import populate_fixture_tree

    return populate_fixture_tree(tmp_path)


@pytest.fixture(params=["path", "uri"])
def flavor(request):
    """The class of path a test runs through: the path of the store that needs
    no URI (`"path"`), or the URI path of its scheme (`"uri"`, skipped without
    uripath). `store_support.make_path()` builds either from one URL."""
    if request.param == "uri":
        pytest.importorskip("uripath")
        pytest.importorskip("objpath.uri")
    return request.param


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: lift the autouse non-loopback network guard for a test "
        "that deliberately resolves or connects to a real host",
    )
    config.addinivalue_line(
        "markers",
        "allow_program(*names): let a test start these programs; any other "
        "except the interpreter and the host probes raises ProcessSpawnBlocked",
    )
    # The session's environment is set before the first test's snapshot of it
    # is taken, and put back when pytest exits.
    home = tempfile.TemporaryDirectory(prefix="objpath-home-")
    patch = pytest.MonkeyPatch()
    hermetic.isolate_environment(patch, home.name)
    config.add_cleanup(home.cleanup)
    config.add_cleanup(patch.undo)


_ENV_BEFORE = pytest.StashKey[dict]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    item.stash[_ENV_BEFORE] = hermetic.environment_snapshot()
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    before = item.stash.get(_ENV_BEFORE, None)
    changed = (
        hermetic.changed_variables(before, hermetic.environment_snapshot())
        if before is not None
        else {}
    )
    if changed:
        hermetic.restore_environment(before)
        raise AssertionError(
            "the test left the process environment changed (use monkeypatch): "
            + ", ".join(f"{k}: {old!r} -> {new!r}" for k, (old, new) in changed.items())
        )
    return result


@pytest.fixture(autouse=True)
def _block_non_loopback_network(request, monkeypatch):
    """Hermetic tests: any connect/sendto/name lookup aimed at a host other
    than loopback raises `NetworkAccessBlocked`, a program other than the
    interpreter raises `ProcessSpawnBlocked`, and a blocked attempt fails the
    test at teardown even if the code under test swallowed the error.
    Autouse rather than opt-in, because the failure it prevents (a refactor
    that quietly sends a monkeypatched HTTP test to the real internet, or a
    DNS lookup that stalls an offline CI runner) is only caught by a guard
    the test author did not have to remember. `@pytest.mark.allow_network`
    lifts the network half for a test that means to use the network."""
    blocked = []
    if not request.node.get_closest_marker("allow_network"):
        hermetic.install_network_guard(monkeypatch, blocked)
    allowed = [
        name
        for marker in request.node.iter_markers("allow_program")
        for name in marker.args
    ]
    hermetic.install_process_guard(monkeypatch, blocked, allowed)
    # Yielded so a test of the guard itself can inspect and clear it.
    yield blocked
    if blocked:
        pytest.fail(f"non-loopback network access attempted: {blocked}")


@pytest.fixture
def unused_tcp_port():
    """A port nobody is listening on, for connection-refused coverage."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def _serve_http(handler):
    """Run a `ThreadingHTTPServer` for `handler` on an OS-assigned loopback
    port; yields its base URL (no trailing slash) and stops it on exit."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # A short poll interval: `shutdown()` waits up to one for the loop to see it.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def serve_http():
    """The `_serve_http(handler)` context manager, for tests that need a
    one-off handler of their own."""
    return _serve_http


@pytest.fixture(autouse=True)
def _fresh_default_object_store_backends():
    """`S3Path` and `GSPath` share one default backend per process; a test
    starts without one, so its client is built for its own mock or fake and
    never inherited from another test."""

    def forget():
        for name in ("_s3_backend", "_gs_backend"):
            module = sys.modules.get(f"objpath.{name}")
            if module is not None:
                module._reset_default_backend()

    forget()
    yield
    forget()


@pytest.fixture
def aws_test_credentials(monkeypatch):
    """The region and credentials a moto-backed client needs; the session
    environment holds no AWS variable of its own."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def s3_server(fixture_tree, aws_test_credentials):
    """In-process moto S3 mock with a pre-populated 'test-bucket' matching
    the standard fixture_tree layout required by ReadPathContract/PathContract."""
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    import pathlib

    def _upload_tree(client, bucket, local_dir, prefix=""):
        local_dir = pathlib.Path(local_dir)
        for entry in local_dir.iterdir():
            rel = (prefix + "/" + entry.name).lstrip("/")
            if entry.is_dir():
                # upload a marker so the directory appears in listings
                client.put_object(Bucket=bucket, Key=rel + "/", Body=b"")
                _upload_tree(client, bucket, entry, rel)
            else:
                client.put_object(Bucket=bucket, Key=rel, Body=entry.read_bytes())

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _upload_tree(client, "test-bucket", fixture_tree)

        yield "s3://test-bucket"


@pytest.fixture
def gcs_api_server(fixture_tree):
    """Faithful (not mocked) fake of the Google Cloud Storage JSON REST API
    subset that `GSPath` actually calls: list, get, put, delete. Yields
    `(base_url, bucket_name)`."""
    import json
    import urllib.parse

    # Build an in-memory object store from fixture_tree
    objects = objstore_fakes.VersionedObjects()
    for item in fixture_tree.rglob("*"):
        if item.is_file():
            rel = item.relative_to(fixture_tree)
            key = str(rel).replace("\\", "/")
            objects[key] = item.read_bytes()
        elif item.is_dir():
            rel = item.relative_to(fixture_tree)
            key = str(rel).replace("\\", "/")
            if key:
                objects[f"{key}/"] = b""

    bucket_name = "test-bucket"
    updated = "2026-07-12T00:00:00Z"
    state = objstore_fakes.FakeState(objects)
    uploads = {}

    class _GcsApiHandler(http.server.BaseHTTPRequestHandler):
        def parse_request(self):
            parsed = super().parse_request()
            if parsed:
                state.requests.append((self.command, self.path))
            return parsed

        def _object_json(self, name):
            content = objects[name]
            described = {
                "kind": "storage#object",
                "name": name,
                "size": str(len(content)),
                "updated": updated,
                "md5Hash": objstore_fakes.md5_base64(content),
                "crc32c": objstore_fakes.crc32c_base64(content),
                "generation": str(objects.version(name)),
            }
            for field, value in state.metadata.get(name, {}).items():
                if value is None:
                    described.pop(field, None)
                else:
                    described[field] = value
            return described

        def _read_upload_body(self, qs):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            upload_type = qs.get("uploadType", [""])[0]
            obj_name = qs.get("name", [""])[0]
            if upload_type == "media":
                return obj_name, body
            if upload_type == "multipart":
                content_type = self.headers.get("Content-Type", "")
                _, _, boundary = content_type.partition("boundary=")
                boundary = boundary.strip().strip('"')
                if not boundary:
                    return obj_name, body
                boundary_bytes = ("--" + boundary).encode()
                content = b""
                for part in body.split(boundary_bytes):
                    part = part.strip()
                    if not part or part == b"--":
                        continue
                    headers_blob, _, payload = part.partition(b"\r\n\r\n")
                    payload = payload.rstrip(b"\r\n")
                    header_text = headers_blob.decode("latin1").lower()
                    if "application/json" in header_text:
                        metadata = json.loads(payload)
                        obj_name = metadata.get("name", obj_name)
                    elif (
                        "application/octet-stream" in header_text
                        or "text/plain" in header_text
                    ):
                        content = payload
                return obj_name, content
            return obj_name, body

        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)

            # GET raw body: /download/storage/v1/b/{bucket}/o/{object}?alt=media
            if split.path.startswith(f"/download/storage/v1/b/{bucket_name}/o/"):
                obj_name = urllib.parse.unquote(
                    split.path[len(f"/download/storage/v1/b/{bucket_name}/o/") :]
                )
                self._send_media(obj_name, qs)
                return

            # LIST: /storage/v1/b/{bucket}/o?prefix=&delimiter=/
            if split.path == f"/storage/v1/b/{bucket_name}/o":
                prefix = qs.get("prefix", [""])[0]
                delimiter = qs.get("delimiter", [None])[0]
                entries = []
                prefixes_set = set()
                for key in sorted(objects.keys()):
                    if key.startswith(prefix):
                        rest = key[len(prefix) :]
                        if delimiter:
                            parts = rest.split(delimiter, 1)
                            if len(parts) > 1:
                                pre = prefix + parts[0] + delimiter
                                if pre not in prefixes_set:
                                    prefixes_set.add(pre)
                                    entries.append(("prefix", pre))
                            else:
                                entries.append(("item", key))
                        else:
                            entries.append(("item", key))
                page, following = objstore_fakes.page_of(
                    entries,
                    int(qs.get("pageToken", ["0"])[0]),
                    int(qs.get("maxResults", ["0"])[0]),
                    state.page_size,
                )
                result = {"kind": "storage#objects", "items": [], "prefixes": []}
                for kind, name in page:
                    if kind == "prefix":
                        result["prefixes"].append(name)
                    else:
                        result["items"].append(self._object_json(name))
                if following is not None:
                    result["nextPageToken"] = str(following)
                self._send_json(200, result)
                return

            # GET raw body: /storage/v1/b/{bucket}/o/{object}?alt=media
            if split.path.startswith(f"/storage/v1/b/{bucket_name}/o/") and qs.get(
                "alt"
            ) == ["media"]:
                obj_name = urllib.parse.unquote(
                    split.path[len(f"/storage/v1/b/{bucket_name}/o/") :]
                )
                self._send_media(obj_name, qs)
                return

            # COPY: /storage/v1/b/{src}/o/{source}/copyTo/b/{dst}/o/{dest}
            copy_prefix = f"/storage/v1/b/{bucket_name}/o/"
            copy_mid = f"/copyTo/b/{bucket_name}/o/"
            if copy_mid in split.path and split.path.startswith(copy_prefix):
                source_name, _, dest_name = split.path[len(copy_prefix) :].partition(
                    copy_mid
                )
                source_name = urllib.parse.unquote(source_name)
                dest_name = urllib.parse.unquote(dest_name)
                if state.copy_status:
                    self._send_json(state.copy_status, {})
                    return
                if source_name in objects:
                    state.copies.append((source_name, dest_name))
                    objects[dest_name] = objects[source_name]
                    self._send_json(200, self._object_json(dest_name))
                    return
                self._send_json(404, {})
                return

            # GET bucket metadata: /storage/v1/b/{bucket}
            if split.path == f"/storage/v1/b/{bucket_name}":
                self._send_json(200, {"kind": "storage#bucket", "name": bucket_name})
                return

            # GET metadata: /storage/v1/b/{bucket}/o/{object}
            if split.path.startswith(f"/storage/v1/b/{bucket_name}/o/"):
                obj_name = urllib.parse.unquote(
                    split.path[len(f"/storage/v1/b/{bucket_name}/o/") :]
                )
                if obj_name in objects:
                    if self._generation_refused(obj_name, qs):
                        self._send_json(412, {})
                        return
                    self._send_json(200, self._object_json(obj_name))
                    return
                self._send_json(404, {})
                return

            self._send_json(404, {})

        def _generation_refused(self, obj_name, qs):
            # ifGenerationMatch: the object must still be the generation asked for.
            required = qs.get("ifGenerationMatch", [None])[0]
            return required is not None and required != str(objects.version(obj_name))

        def _send_media(self, obj_name, qs):
            # The object's bytes, or the range of them that `Range` asks for.
            if state.before_content_read is not None:
                state.before_content_read(obj_name)
            if obj_name not in objects:
                self._send_json(404, {})
                return
            required = qs.get("ifGenerationMatch", [None])[0]
            wanted = self.headers.get("Range", "")
            first = wanted[6:].partition("-")[0] if wanted.startswith("bytes=") else ""
            state.reads.append((obj_name, int(first or 0), required))
            if self._generation_refused(obj_name, qs):
                self._send_json(412, {})
                return
            content = objects[obj_name]
            if wanted.startswith("bytes="):
                first, _, last = wanted[6:].partition("-")
                start = int(first or 0)
                if start >= len(content):
                    self._send_json(416, {})
                    return
                end = min(int(last) if last else len(content) - 1, len(content) - 1)
                body = content[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
            else:
                body = content
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Goog-Generation", str(objects.version(obj_name)))
            self.end_headers()
            self.wfile.write(body)

        def _resumable_start(self, qs):
            # A resumable upload begins with a JSON body naming the object
            # and is answered with the URL that takes its chunks.
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            name = (json.loads(body) if body else {}).get("name") or qs.get(
                "name", [""]
            )[0]
            if qs.get("ifGenerationMatch") == ["0"] and name in objects:
                self._send_json(412, {})
                return
            upload_id = f"upload-{len(uploads)}"
            uploads[upload_id] = (name, bytearray(), hashlib.md5(), [0, 0])
            self.send_response(200)
            self.send_header(
                "Location",
                f"http://{self.headers['Host']}/upload/storage/v1/b/{bucket_name}"
                f"/o?uploadType=resumable&upload_id={upload_id}",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _resumable_chunk(self, qs):
            name, received, digest, counted = uploads[qs["upload_id"][0]]
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            span, _, total = (
                self.headers["Content-Range"].partition(" ")[2].partition("/")
            )
            if span != "*":
                assert int(span.split("-")[0]) == counted[0], "chunk out of order"
                counted[0] += len(body)
                counted[1] = objstore_fakes.crc32c(body, counted[1])
                digest.update(body)
                if not state.discard_uploads:
                    received.extend(body)
            if total != "*" and counted[0] == int(total):
                if state.discard_uploads:
                    objects[name] = b""
                    state.discarded[name] = (counted[0], digest.hexdigest())
                    state.metadata[name] = {
                        "size": str(counted[0]),
                        "md5Hash": base64.b64encode(digest.digest()).decode(),
                        "crc32c": base64.b64encode(
                            counted[1].to_bytes(4, "big")
                        ).decode(),
                    }
                else:
                    objects[name] = bytes(received)
                self._send_json(200, self._object_json(name))
                return
            self.send_response(308)
            if counted[0]:
                self.send_header("Range", f"bytes=0-{counted[0] - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)

            if split.path == f"/upload/storage/v1/b/{bucket_name}/o":
                if qs.get("uploadType") == ["resumable"]:
                    self._resumable_start(qs)
                    return
                obj_name, content = self._read_upload_body(qs)
                if qs.get("ifGenerationMatch") == ["0"] and obj_name in objects:
                    self._send_json(412, {})
                    return
                if obj_name:
                    objects[obj_name] = content
                    self._send_json(200, self._object_json(obj_name))
                    return
                self._send_json(400, {})
                return

            if split.path == "/batch/storage/v1":
                self._serve_batch()
                return

            self.do_GET()

        def _serve_batch(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if state.batch_status != 200:
                self._send_json(state.batch_status, {})
                return
            names = objstore_fakes.gcs_batch_deletes(
                self.headers.get("Content-Type", ""), body
            )
            state.batches.append(names)
            statuses = []
            for name in names:
                if name in state.refuse:
                    statuses.append(state.refuse[name])
                elif name in objects:
                    del objects[name]
                    statuses.append(204)
                else:
                    statuses.append(404)
            reply, content_type = objstore_fakes.gcs_batch_reply(statuses)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def do_PUT(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if "upload_id" in qs:
                self._resumable_chunk(qs)
                return
            self.do_POST()

        def do_DELETE(self):
            split = urllib.parse.urlsplit(self.path)
            if split.path.startswith(f"/storage/v1/b/{bucket_name}/o/"):
                obj_name = urllib.parse.unquote(
                    split.path[len(f"/storage/v1/b/{bucket_name}/o/") :]
                )
                if obj_name in state.refuse:
                    self._send_json(state.refuse[obj_name], {})
                    return
                if obj_name in objects:
                    del objects[obj_name]
                    self.send_response(204)
                    self.end_headers()
                    return
                self._send_json(404, {})
                return
            self._send_json(404, {})

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with _serve_http(_GcsApiHandler) as base_url:
        yield objstore_fakes.ServerInfo((base_url, bucket_name), state)


@pytest.fixture
def gs_server(gcs_api_server, flavor):
    """The root of the fixture bucket as a path of `flavor`, and the backend
    that reaches the in-process GCS fake."""
    from objpath import GSBackend
    from store_support import make_path

    base_url, bucket_name = gcs_api_server
    # The SDK authenticates against a custom endpoint unless told not to.
    backend = GSBackend(
        client_options={"api_endpoint": base_url}, use_auth_w_custom_endpoint=False
    )
    return make_path(flavor, f"gs://{bucket_name}", backend=backend), backend


@pytest.fixture
def az_api_server(fixture_tree):
    """Faithful (not mocked) fake of the Azure Blob Storage XML REST API
    subset that `AZPath` actually calls. Yields `(base_url, account, container)`."""
    import urllib.parse
    from xml.etree import ElementTree as ET

    objects = objstore_fakes.VersionedObjects()
    for item in fixture_tree.rglob("*"):
        if item.is_file():
            rel = item.relative_to(fixture_tree)
            key = str(rel).replace("\\", "/")
            objects[key] = item.read_bytes()
        elif item.is_dir():
            rel = item.relative_to(fixture_tree)
            key = str(rel).replace("\\", "/")
            if key:
                objects[f"{key}/"] = b""

    account = "testaccount"
    container = "testcontainer"
    last_modified = "Sun, 12 Jul 2026 00:00:00 GMT"
    state = objstore_fakes.FakeState(objects)
    blocks = {}
    discards = {}

    def _copy_status_headers():
        return {
            "x-ms-copy-id": "copy-id",
            "x-ms-copy-status": "success",
        }

    class _AzApiHandler(http.server.BaseHTTPRequestHandler):
        def parse_request(self):
            parsed = super().parse_request()
            if parsed:
                state.requests.append((self.command, self.path))
            return parsed

        def _md5(self, name):
            # The Content-MD5 the service reports for `name`, or None.
            overrides = state.metadata.get(name, {})
            if "Content-MD5" in overrides:
                return overrides["Content-MD5"]
            return objstore_fakes.md5_base64(objects[name])

        def _send_blob_headers(self, length, name=None):
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Last-Modified", last_modified)
            self.send_header("ETag", f'"{objects.version(name)}"')
            self.send_header("x-ms-blob-type", "BlockBlob")
            if name is not None and self._md5(name):
                self.send_header("Content-MD5", self._md5(name))

        def _blob_element(self, blobs_elem, key):
            blob_elem = ET.SubElement(blobs_elem, "Blob")
            ET.SubElement(blob_elem, "Name").text = key
            props_elem = ET.SubElement(blob_elem, "Properties")
            ET.SubElement(props_elem, "Content-Length").text = str(len(objects[key]))
            ET.SubElement(props_elem, "Last-Modified").text = last_modified
            ET.SubElement(props_elem, "BlobType").text = "BlockBlob"
            if self._md5(key):
                ET.SubElement(props_elem, "Content-MD5").text = self._md5(key)

        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)

            # LIST: /{account}/{container}?restype=container&comp=list
            if (
                split.path == f"/{account}/{container}"
                and qs.get("restype") == ["container"]
                and qs.get("comp") == ["list"]
            ):
                prefix = qs.get("prefix", [""])[0]
                delimiter = qs.get("delimiter", [None])[0]

                root = ET.Element("EnumerationResults")
                root.set("ContainerName", container)
                # The SDK repeats these on the request for the next page.
                if prefix:
                    ET.SubElement(root, "Prefix").text = prefix
                if delimiter:
                    ET.SubElement(root, "Delimiter").text = delimiter

                blobs_elem = ET.SubElement(root, "Blobs")
                prefixes_set = set()
                entries = []

                for key in sorted(objects.keys()):
                    if key.startswith(prefix):
                        rest = key[len(prefix) :]
                        if delimiter:
                            parts = rest.split(delimiter, 1)
                            if len(parts) > 1:
                                pre = prefix + parts[0] + delimiter
                                if pre not in prefixes_set:
                                    prefixes_set.add(pre)
                                    entries.append(("prefix", pre))
                            else:
                                entries.append(("blob", key))
                        else:
                            entries.append(("blob", key))

                page, following = objstore_fakes.page_of(
                    entries,
                    int(qs.get("marker", ["0"])[0] or 0),
                    int(qs.get("maxresults", ["0"])[0]),
                    state.page_size,
                )
                for kind, name in page:
                    if kind == "prefix":
                        pre_elem = ET.SubElement(blobs_elem, "BlobPrefix")
                        ET.SubElement(pre_elem, "Name").text = name
                    else:
                        self._blob_element(blobs_elem, name)

                ET.SubElement(root, "NextMarker").text = (
                    "" if following is None else str(following)
                )

                body = ET.tostring(root, encoding="utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # GET blob or HEAD: /{account}/{container}/{blob}
            if split.path.startswith(f"/{account}/{container}/"):
                blob_name = urllib.parse.unquote(
                    split.path[len(f"/{account}/{container}/") :]
                )
                if self.command != "HEAD" and state.before_content_read:
                    state.before_content_read(blob_name)
                if blob_name in objects:
                    required = self.headers.get("If-Match")
                    if self.command != "HEAD":
                        wanted = self.headers.get("x-ms-range", "")
                        first = wanted[6:].partition("-")[0]
                        state.reads.append((blob_name, int(first or 0), required))
                    if (
                        required is not None
                        and required != f'"{objects.version(blob_name)}"'
                    ):
                        self.send_response(412)
                        self.send_header("x-ms-error-code", "ConditionNotMet")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    content = objects[blob_name]
                    range_header = self.headers.get("x-ms-range") or self.headers.get(
                        "Range"
                    )
                    start = 0
                    end = len(content) - 1
                    status = 200
                    if range_header and range_header.startswith("bytes="):
                        start_text, _, end_text = range_header[6:].partition("-")
                        if start_text:
                            start = int(start_text)
                        if end_text:
                            end = int(end_text)
                        status = 206
                    chunk = content[start : end + 1]
                    self.send_response(status)
                    self._send_blob_headers(len(chunk), blob_name)
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{len(content)}"
                    )
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(chunk)
                    return
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

        def do_HEAD(self):
            # Delegate to GET
            self.do_GET()

        def do_POST(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if qs.get("comp") != ["batch"] or state.batch_status != 200:
                self.send_response(
                    400 if qs.get("comp") != ["batch"] else state.batch_status
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            names = objstore_fakes.azure_batch_deletes(account, container, body)
            state.batches.append(names)
            statuses = []
            for name in names:
                if name in state.refuse:
                    statuses.append(state.refuse[name])
                elif name in objects:
                    del objects[name]
                    statuses.append(202)
                else:
                    statuses.append(404)
            reply, content_type = objstore_fakes.azure_batch_reply(statuses)
            self.send_response(202)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def do_PUT(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)

            if split.path.startswith(f"/{account}/{container}/"):
                blob_name = urllib.parse.unquote(
                    split.path[len(f"/{account}/{container}/") :]
                )
                if qs.get("comp") == ["copy"] or self.headers.get("x-ms-copy-source"):
                    if state.copy_status:
                        self.send_response(state.copy_status)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    source = self.headers.get("x-ms-copy-source", "")
                    source_path = urllib.parse.urlsplit(source).path
                    source_prefix = f"/{account}/{container}/"
                    if source_path.startswith(source_prefix):
                        source_name = urllib.parse.unquote(
                            source_path[len(source_prefix) :]
                        )
                        if source_name in objects:
                            state.copies.append((source_name, blob_name))
                            objects[blob_name] = objects[source_name]
                            self.send_response(202)
                            for header, value in _copy_status_headers().items():
                                self.send_header(header, value)
                            self.end_headers()
                            return
                    self.send_response(404)
                    self.end_headers()
                    return

                content_length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(content_length)
                if qs.get("comp") == ["block"]:
                    if state.discard_uploads:
                        # Blocks arrive in the order they are committed.
                        kept = discards.setdefault(blob_name, [0, hashlib.md5()])
                        kept[0] += len(data)
                        kept[1].update(data)
                        blocks[(blob_name, qs["blockid"][0])] = b""
                    else:
                        blocks[(blob_name, qs["blockid"][0])] = data
                    self.send_response(201)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("If-None-Match") == "*" and blob_name in objects:
                    self.send_response(409)
                    self.send_header("x-ms-error-code", "BlobAlreadyExists")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if qs.get("comp") == ["blocklist"]:
                    ids = [
                        element.text
                        for element in ET.fromstring(data)
                        if element.tag in ("Latest", "Committed", "Uncommitted")
                    ]
                    data = b"".join(blocks.pop((blob_name, one)) for one in ids)
                    if state.discard_uploads:
                        total, digest = discards.pop(blob_name)
                        state.discarded[blob_name] = (total, digest.hexdigest())
                objects[blob_name] = data
                self.send_response(201)
                self.send_header("Last-Modified", last_modified)
                self.send_header("ETag", f'"{objects.version(blob_name)}"')
                self.end_headers()
                return

            self.send_response(400)
            self.end_headers()

        def do_DELETE(self):
            split = urllib.parse.urlsplit(self.path)

            if split.path.startswith(f"/{account}/{container}/"):
                blob_name = urllib.parse.unquote(
                    split.path[len(f"/{account}/{container}/") :]
                )
                if blob_name in state.refuse:
                    self.send_response(state.refuse[blob_name])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if blob_name in objects:
                    del objects[blob_name]
                    self.send_response(202)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    with _serve_http(_AzApiHandler) as base_url:
        yield objstore_fakes.ServerInfo((base_url, account, container), state)


@pytest.fixture
def az_server(az_api_server, flavor):
    """The root of the fixture container as a path of `flavor`, and the backend
    that reaches the in-process Azure fake."""
    from objpath import AZBackend
    from store_support import make_path

    base_url, account, container = az_api_server
    conn_str = (
        f"DefaultEndpointsProtocol=http;AccountName={account};"
        f"AccountKey=dGVzdGtleQ==;BlobEndpoint={base_url}/{account};"
    )
    backend = AZBackend(connection_string=conn_str)
    return make_path(flavor, f"az://{account}/{container}", backend=backend), backend
