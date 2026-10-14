# Benchmarks

`bench.py` times `S3Path` against an S3 bucket that `moto` holds in the benchmark's own
process: a warm `stat()`, `iterdir()` and `walk()` of 72 entries, `read_bytes()` and
`write_bytes()` of 256 KiB, `mkdir()`, `rename()`, `unlink()`, a server-side `copy()` and
`rm(recursive=True)` of a 33-object tree, with the number of requests the removal sends
recorded beside the timing. The same operations run again through the `s3:` scheme of
`uripath` when it is installed. Method notes are on the docs site's Benchmarks page
([`docs/benchmarks.md`](../docs/benchmarks.md)).

Nothing here contacts an external host: the bucket exists only in the process, with fake
credentials.

## Reproduce

From the repository root, in a virtualenv with `boto3`, `moto` and `uripath` (the `dev`
and `s3`, `uri` extras):

```bash
python -m pip install -e ".[dev,s3,uri]"
python benchmarks/bench.py --save
```

That runs the suite and writes
`benchmarks/results/<version>-py<major.minor>-<os>-<arch>.json`:

```bash
python benchmarks/bench.py --help
python benchmarks/bench.py --save --samples 9
python benchmarks/bench.py --save --name my-run
```

| Option | Effect |
| --- | --- |
| `--save` | Write the run to `benchmarks/results/<name>.json`. |
| `--name NAME` | File name without `.json`. Default `<version>-py<major.minor>-<os>-<arch>`. |
| `--results-dir DIR` | Write somewhere other than `benchmarks/results/`. |
| `--samples N` | Samples per metric for every case (1-100). Default: 5. |
| `--note TEXT` | Free text stored in the JSON. |

Without `uripath` the `s3-uri` group is skipped and the reason is recorded in `skipped`.

## Result schema

`schema` is `objpath.bench/1`. A result holds `name`, `suite`, `source` (`ci` when run by
GitHub Actions, else `local`), `note`, `created_utc`, `package` (`name`, `version`), `git`
(`commit`, `dirty`), `python`, `interpreter` (`<major.minor>-<os>-<arch>`), `processor`,
`cpu_count`, and four maps keyed by the metric name:

| Key | Holds |
| --- | --- |
| `metrics` | per metric: `group` (`s3` or `s3-uri`), `min_ms`, `median_ms` and `max_ms` per call, `samples`, `calls_per_sample` |
| `counters` | per counter: `group` and the number of requests of each S3 operation |
| `errors` | per metric that raised: `group` and the error |
| `skipped` | per metric not run: `group` and the reason |

Compare two runs on their medians, and only between runs from the same machine and
interpreter. A run on a developer machine is a sanity check, not a performance claim:
performance claims in the changelog or release notes come from CI runs (`test.yml` with
`benchmark` set).

## Committed results

`results/baseline-local-*.json` are local sanity baselines of the suite on one developer
machine, for 3.14 and 3.9.
