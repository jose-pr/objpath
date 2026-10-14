"""objpath benchmark runner.

Run `python benchmarks/bench.py --help` for the suites. `--save` writes the run
as structured JSON into `benchmarks/results/`; the schema is documented in
`benchmarks/README.md`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import itertools
import json
import os
import pathlib
import platform
import re
import statistics
import subprocess
import sys
import sysconfig
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# The checkout's sources win over an installed copy: a run measures the tree it
# is started from.
sys.path.insert(0, str(REPO_ROOT / "src"))

SCHEMA = "objpath.bench/1"
MAX_SAMPLES = 100

#: The groups of the saved metrics: `S3Path`, and the same operations through
#: the `s3:` scheme of `uripath`.
GROUP = "s3"
URI_GROUP = "s3-uri"

BUCKET = "bench"
LISTED = 72
TREE = 33


class _Recorder:
    """Collects every measured metric of one run for `--save`.

    A timing metric stores min/median/max milliseconds per call over its
    samples; each sample times `calls_per_sample` calls of the operation.
    """

    def __init__(self):
        self.metrics = {}
        self.counters = {}
        self.errors = {}
        self.skipped = {}

    def timing(self, name, samples, *, group, calls=1):
        per_call_ms = sorted(sample / calls * 1000.0 for sample in samples)
        self.metrics[name] = {
            "group": group,
            "min_ms": round(per_call_ms[0], 6),
            "median_ms": round(statistics.median(per_call_ms), 6),
            "max_ms": round(per_call_ms[-1], 6),
            "samples": len(per_call_ms),
            "calls_per_sample": calls,
        }

    def counter(self, name, values, *, group):
        self.counters[name] = {"group": group, **values}

    def error(self, name, message, *, group):
        self.errors[name] = {"group": group, "error": message}

    def skip(self, name, reason, *, group):
        self.skipped[name] = {"group": group, "reason": reason}


RECORDER = _Recorder()
_SAMPLES_OVERRIDE = None


def _reps(default):
    """Sample count for a case: `--samples` when given, else the case default."""
    return _SAMPLES_OVERRIDE or default


class _Numbered:
    """An operation that needs a name of its own on every call.

    `before()` numbers the next call and runs `prepare(n)`, which is not timed;
    calling the object runs `operate(n)`. The warm-up call counts as one.
    """

    def __init__(self, operate, prepare=None):
        self._operate = operate
        self._prepare = prepare
        self._numbers = itertools.count()
        self._current = -1

    def before(self):
        self._current = next(self._numbers)
        if self._prepare is not None:
            self._prepare(self._current)

    def __call__(self):
        self._operate(self._current)


def _measure(operation, *, repeat=5, name, group):
    """Time `repeat` samples of `operation` after one warm-up call, and record
    them. An operation with a `before` attribute has it called, untimed, ahead
    of the warm-up call and of every sample. An exception is recorded as an
    error."""
    before = getattr(operation, "before", None)

    def timed():
        if before is not None:
            before()
        start = time.perf_counter()
        operation()
        return time.perf_counter() - start

    try:
        timed()
        samples = [timed() for _ in range(repeat)]
    except Exception as error:
        RECORDER.error(name, f"{type(error).__name__}: {error}", group=group)
        return None
    RECORDER.timing(name, samples, group=group)
    return statistics.median(samples)


@contextlib.contextmanager
def _moto_bucket():
    """An S3 bucket held in this process by `moto`, with fake credentials."""
    import boto3
    from moto import mock_aws

    saved = {
        name: os.environ.get(name)
        for name in ("AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    }
    os.environ.update(
        AWS_DEFAULT_REGION="us-east-1",
        AWS_ACCESS_KEY_ID="bench",
        AWS_SECRET_ACCESS_KEY="bench",
    )
    try:
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket=BUCKET)
            yield client
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _make_path(group, key, backend):
    if group == URI_GROUP:
        from uripath import URIPath

        return URIPath(f"s3://{BUCKET}/{key}", backend=backend)
    from objpath import S3Path

    return S3Path(key, bucket=BUCKET, backend=backend)


def _seed(client):
    for number in range(LISTED):
        client.put_object(Bucket=BUCKET, Key=f"listed/f{number:03}.txt", Body=b"x")
    client.put_object(Bucket=BUCKET, Key="one.txt", Body=b"x" * 1024)
    client.put_object(Bucket=BUCKET, Key="big.bin", Body=b"y" * 256 * 1024)


def _cases(group, client, backend):
    """`(label, operation)` pairs for one group, each an operation on paths of
    that group."""
    path = lambda key: _make_path(group, key, backend)  # noqa: E731
    payload = b"z" * 256 * 1024

    def populate(number):
        client.put_object(Bucket=BUCKET, Key=f"scratch/{number}.bin", Body=payload)

    def populate_tree(number):
        for child in range(TREE):
            client.put_object(
                Bucket=BUCKET, Key=f"tree{number}/d{child % 3}/f{child}.txt", Body=b"x"
            )

    yield "stat() of an object", lambda: path("one.txt").stat()
    yield f"iterdir() of {LISTED} entries", lambda: list(path("listed").iterdir())
    yield f"walk() of {LISTED} entries", lambda: list(path("listed").walk())
    yield "read_bytes() of 256 KiB", lambda: path("big.bin").read_bytes()
    yield "write_bytes() of 256 KiB", lambda: path("out/w.bin").write_bytes(payload)
    yield "mkdir()", _Numbered(lambda n: path(f"dirs/{group}-{n}").mkdir())
    yield "rename() of 256 KiB", _Numbered(
        lambda n: path(f"scratch/{n}.bin").rename(f"{n}-renamed.bin"), populate
    )
    yield "unlink()", _Numbered(lambda n: path(f"scratch/{n}.bin").unlink(), populate)
    yield "copy() of 256 KiB, server side", _Numbered(
        lambda n: path("big.bin").copy(path(f"copies/{group}-{n}.bin")), None
    )
    yield f"rm(recursive=True) of {TREE} objects", _Numbered(
        lambda n: path(f"tree{n}").rm(recursive=True), populate_tree
    )


class _Counting:
    """A botocore event handler that counts the requests of each operation."""

    def __init__(self, client):
        self.calls = {}
        client.meta.events.register("before-call.s3.*", self._count)

    def _count(self, model, **kwargs):
        self.calls[model.name] = self.calls.get(model.name, 0) + 1


def benchmark_s3():
    from objpath import S3Backend

    groups = [GROUP]
    try:
        import uripath  # noqa: F401
        import objpath.uri  # noqa: F401
    except ImportError as error:
        RECORDER.skip("s3-uri", f"uripath is not installed ({error})", group=URI_GROUP)
    else:
        groups.append(URI_GROUP)
    results = {}
    with _moto_bucket() as client:
        _seed(client)
        backend = S3Backend()
        backend._client = client
        counting = _Counting(client)
        for group in groups:
            for label, operation in _cases(group, client, backend):
                results[(group, label)] = _measure(
                    operation, repeat=_reps(5), name=f"{group}: {label}", group=group
                )
            counting.calls.clear()
            tree = _Numbered(
                lambda n: _make_path(group, f"counted{n}", backend).rm(recursive=True),
                lambda n: [
                    client.put_object(Bucket=BUCKET, Key=f"counted{n}/f{i}", Body=b"x")
                    for i in range(TREE)
                ],
            )
            tree.before()
            counting.calls.clear()
            tree()
            RECORDER.counter(
                f"{group}: requests of rm(recursive=True) of {TREE} objects",
                dict(counting.calls),
                group=group,
            )
    return results


def _format(value):
    return "n/a" if value is None else f"{value * 1000:.3f} ms"


def print_results(results, *, markdown=False):
    groups = sorted({group for group, _label in results})
    labels = []
    for _group, label in results:
        if label not in labels:
            labels.append(label)
    if markdown:
        print("| Operation | " + " | ".join(groups) + " |")
        print("| --- | " + " | ".join("---" for _ in groups) + " |")
    for label in labels:
        cells = [_format(results.get((group, label))) for group in groups]
        if markdown:
            print(f"| {label} | " + " | ".join(cells) + " |")
        else:
            print(f"{label}: " + ", ".join(f"{g} {c}" for g, c in zip(groups, cells)))


def main():
    print("Running benchmarks...")
    results = benchmark_s3()
    print_results(results)
    # Markdown table format, for copy-pasting.
    print()
    print_results(results, markdown=True)


# --- the saved result --------------------------------------------------------


def _pyproject_version():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _git_state():
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": commit, "dirty": dirty}


def _interpreter_tag():
    """`<major.minor>-<os>-<arch>`, with the arch the interpreter was built for."""
    arch = sysconfig.get_platform().rsplit("-", 1)[-1]
    os_name = "darwin" if sys.platform == "darwin" else os.name
    return f"{sys.version_info[0]}.{sys.version_info[1]}-{os_name}-{arch}"


def build_result(suite, *, name, note=None):
    """The JSON document `--save` writes (schema: benchmarks/README.md)."""
    return {
        "schema": SCHEMA,
        "name": name,
        "suite": suite,
        "source": "ci" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
        "note": note,
        "created_utc": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "package": {"name": "objpath", "version": _pyproject_version()},
        "git": _git_state(),
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "interpreter": _interpreter_tag(),
        "processor": f"{platform.system()}-{platform.machine()}",
        "cpu_count": os.cpu_count(),
        "metrics": dict(sorted(RECORDER.metrics.items())),
        "counters": dict(sorted(RECORDER.counters.items())),
        "errors": dict(sorted(RECORDER.errors.items())),
        "skipped": dict(sorted(RECORDER.skipped.items())),
    }


def default_result_name(suite):
    name = f"{_pyproject_version()}-py{_interpreter_tag()}"
    return name if suite == "default" else f"{name}-{suite}"


def save_result(suite, *, name=None, results_dir=None, note=None):
    name = name or default_result_name(suite)
    result = build_result(suite, name=name, note=note)
    directory = (
        pathlib.Path(results_dir)
        if results_dir
        else REPO_ROOT / "benchmarks" / "results"
    )
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    with open(target, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(f"\nSaved {len(result['metrics'])} metrics to {target}")
    return target


def _samples_arg(value):
    count = int(value)
    if not 1 <= count <= MAX_SAMPLES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_SAMPLES}")
    return count


def cli(argv=None):
    global _SAMPLES_OVERRIDE

    parser = argparse.ArgumentParser(
        description="Run objpath benchmarks: S3Path against an S3 bucket held "
        "in this process by moto, and the s3: scheme of uripath when installed.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="write the run as JSON to benchmarks/results/<name>.json",
    )
    parser.add_argument(
        "--name",
        help="result name for --save, without .json "
        "(default: <version>-py<major.minor>-<os>-<arch>)",
    )
    parser.add_argument(
        "--results-dir",
        help="directory for --save (default: the benchmarks/results directory beside this script)",
    )
    parser.add_argument(
        "--samples",
        type=_samples_arg,
        help=f"samples per metric for every case, 1-{MAX_SAMPLES} "
        "(default: each case's own count, 5)",
    )
    parser.add_argument("--note", help="free-text note stored in the saved JSON")
    args = parser.parse_args(argv)
    _SAMPLES_OVERRIDE = args.samples

    main()
    if args.save:
        save_result(
            "default", name=args.name, results_dir=args.results_dir, note=args.note
        )


if __name__ == "__main__":
    cli()
