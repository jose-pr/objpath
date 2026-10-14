"""The digests a store reports."""

from __future__ import annotations

import base64 as _base64


def digest_hex(encoded: str) -> str:
    """The hex digest of `encoded`, the base64 text a store reports one in."""
    return _base64.b64decode(encoded).hex()
