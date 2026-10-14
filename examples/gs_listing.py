"""List keys in a Google Cloud Storage bucket using `GSPath`.

Needs the `gs` extra (`pip install "objpath[gs]"`) and Application Default
Credentials. Importing this module is always safe; run as a script it does
nothing, and exits 0, unless GS_EXAMPLE_BUCKET is set.

    export GS_EXAMPLE_BUCKET=my-gcs-bucket
    export GS_EXAMPLE_PREFIX=some/folder/   # optional
    python examples/gs_listing.py
"""

import os
import sys

from objpath import GSPath


def list_gs(bucket: str, prefix: str):
    root = GSPath(prefix, bucket=bucket)
    print(f"Listing GCS location: {root}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    bucket = os.environ.get("GS_EXAMPLE_BUCKET")
    if not bucket:
        print(
            "GS_EXAMPLE_BUCKET is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    prefix = os.environ.get("GS_EXAMPLE_PREFIX", "")

    try:
        list_gs(bucket, prefix)
    except Exception as error:
        print(
            f"Could not connect to GCS bucket {bucket} ({error}); skipping.",
            file=sys.stderr,
        )
