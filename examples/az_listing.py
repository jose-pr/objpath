"""List blobs in an Azure Blob Storage container using `AZPath`.

Needs the `az` extra (`pip install "objpath[az]"`) and credentials that
azure-identity's DefaultAzureCredential finds. Importing this module is always
safe; run as a script it does nothing, and exits 0, unless AZ_EXAMPLE_ACCOUNT
and AZ_EXAMPLE_CONTAINER are set.

    export AZ_EXAMPLE_ACCOUNT=mystorageaccount
    export AZ_EXAMPLE_CONTAINER=mycontainer
    export AZ_EXAMPLE_PREFIX=some/folder/   # optional
    python examples/az_listing.py
"""

import os
import sys

from objpath import AZPath


def list_az(account: str, container: str, prefix: str):
    root = AZPath(prefix, container=container, account=account)
    print(f"Listing Azure location: {root}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    account = os.environ.get("AZ_EXAMPLE_ACCOUNT")
    container = os.environ.get("AZ_EXAMPLE_CONTAINER")
    if not account or not container:
        print(
            "AZ_EXAMPLE_ACCOUNT and AZ_EXAMPLE_CONTAINER are not set -- "
            "skipping. See this file's module docstring for the required "
            "environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    prefix = os.environ.get("AZ_EXAMPLE_PREFIX", "")

    try:
        list_az(account, container, prefix)
    except Exception as error:
        print(
            f"Could not connect to container {container} ({error}); skipping.",
            file=sys.stderr,
        )
