"""The bucket or container a path is in, and how a pickled path gets it back."""

from __future__ import annotations


def check_place(kind: str, value, donor, owner: str) -> str:
    """The name of the bucket or container (`kind`) that the constructor of
    `owner` was given as `value`, else the one of the path `donor` it is
    joined from. `TypeError` when there is none or it is not a string,
    `ValueError` when it is empty or holds a `/`."""
    if value is None:
        if donor is None:
            raise TypeError(f"{owner} needs {kind}= (or a segment that is an {owner})")
        return donor._place
    if not isinstance(value, str):
        raise TypeError(f"{kind}= must be a str, not {type(value).__name__!r}")
    if not value or "/" in value:
        raise ValueError(f"{kind}= must be a name with no '/' in it, not {value!r}")
    return value


def rebuild_path(cls, segments, keywords):
    """Unpickles a path: `cls(**keywords)` with the names it had."""
    path = cls(**keywords)
    path._segments = segments
    return path
