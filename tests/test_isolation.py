"""What importing and building paths needs, and what it never loads.

Each case runs in a fresh interpreter that blocks the import of the named
modules, so nothing is uninstalled and nothing connects."""

from __future__ import annotations

import pytest

from child import run

SDKS = {
    "s3": ["boto3", "botocore"],
    "gs": ["google", "google.cloud", "google.cloud.storage", "google.api_core"],
    "az": [
        "azure",
        "azure.core",
        "azure.identity",
        "azure.storage",
        "azure.storage.blob",
    ],
}
EVERY_SDK = [name for names in SDKS.values() for name in names]


def test_importing_the_package_loads_no_sdk_and_no_uripath():
    result = run("""
        import sys
        import objpath

        loaded = {"boto3", "botocore", "google", "azure", "uripath", "objpath.uri"}
        assert not loaded & set(sys.modules), loaded & set(sys.modules)
        assert objpath.S3Path and objpath.GSPath and objpath.AZPath
        print("ok")
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_naming_a_store_loads_the_other_stores_sdks_never():
    result = run(
        """
        import sys
        from objpath import S3Path

        loaded = [name for name in ("google", "azure") if sys.modules.get(name)]
        assert not loaded, loaded
        print("ok")
        """,
        ["google", "azure"],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_building_and_deriving_paths_needs_no_sdk():
    result = run(
        """
        from objpath import (
            AZPath, BaseAZBackend, BaseGSBackend, BaseS3Backend, GSPath, S3Path,
        )

        class Refusing:
            def client(self):
                raise AssertionError("a pure operation asked for a client")

        class S3(Refusing, BaseS3Backend): pass
        class GS(Refusing, BaseGSBackend): pass
        class AZ(Refusing, BaseAZBackend): pass

        for path in (
            S3Path("data/x", bucket="b", backend=S3()),
            GSPath("data/x", bucket="b", backend=GS()),
            AZPath("data/x", container="c", backend=AZ()),
            S3Path("data/x", bucket="b", region_name="eu-west-1"),
            AZPath("data/x", container="c", account="acct"),
        ):
            derived = [path / "y", path.parent, path.with_name("z"), path.joinpath("a", "b")]
            assert all(type(p) is type(path) and p.backend is path.backend for p in derived)
            assert derived[0].key == "data/x/y" and path.key == "data/x"
            str(path), repr(path), hash(path)
        print("ok")
        """,
        EVERY_SDK,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("store, extra", [("s3", "s3"), ("gs", "gs"), ("az", "az")])
def test_the_first_request_without_the_sdk_names_the_extra(store, extra):
    builders = {
        "s3": 'S3Path("k", bucket="b")',
        "gs": 'GSPath("k", bucket="b")',
        "az": 'AZPath("k", container="c", account="acct")',
    }
    result = run(
        f"""
        from objpath import S3Path, GSPath, AZPath

        path = {builders[store]}
        try:
            path.stat()
        except ImportError as error:
            print(error)
        """,
        SDKS[store],
    )
    assert result.returncode == 0, result.stderr
    assert f'pip install "objpath[{extra}]"' in result.stdout


def test_the_uri_module_without_uripath_names_the_uri_extra():
    result = run(
        """
        try:
            import objpath.uri
        except ImportError as error:
            print(error, error.name)
        """,
        ["uripath"],
    )
    assert result.returncode == 0, result.stderr
    assert 'pip install "objpath[uri]"' in result.stdout
    assert result.stdout.split()[-1] == "uripath"


def test_importing_the_uri_module_loads_no_sdk():
    pytest.importorskip("uripath")
    result = run("""
        import sys
        import objpath.uri

        loaded = {"boto3", "botocore", "google", "azure"} & set(sys.modules)
        assert not loaded, loaded
        print("ok")
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
