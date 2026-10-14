"""List keys in an S3 bucket using `S3Path`.

Needs the `s3` extra (`pip install "objpath[s3]"`) and AWS credentials that
boto3 finds (environment variables, the AWS CLI configuration or an IAM role).
Importing this module is always safe; run as a script it does nothing, and
exits 0, unless S3_EXAMPLE_BUCKET is set.

    export S3_EXAMPLE_BUCKET=my-s3-bucket
    export S3_EXAMPLE_PREFIX=some/folder/   # optional
    python examples/s3_listing.py
"""

import os
import sys

from objpath import S3Path


def list_s3(bucket: str, prefix: str):
    root = S3Path(prefix, bucket=bucket)
    print(f"Listing S3 location: {root}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    bucket = os.environ.get("S3_EXAMPLE_BUCKET")
    if not bucket:
        print(
            "S3_EXAMPLE_BUCKET is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    prefix = os.environ.get("S3_EXAMPLE_PREFIX", "")

    try:
        list_s3(bucket, prefix)
    except Exception as error:
        print(
            f"Could not connect to S3 bucket {bucket} ({error}); skipping.",
            file=sys.stderr,
        )
