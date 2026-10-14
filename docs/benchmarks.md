# Benchmarks

`objpath` includes a benchmark harness at `benchmarks/bench.py` that times `S3Path`, and the
`s3:` scheme of `uripath` on the same operations. It needs `moto` and `boto3`:
`pip install -e ".[dev,s3,uri]"`.

## Run It

```bash
python benchmarks/bench.py            # run the benchmarks
python benchmarks/bench.py --help     # options; runs nothing
```

## Saved Results

Every metric is timed over several samples. `--save` writes the run as JSON to
`benchmarks/results/<name>.json`, with the minimum, median and maximum milliseconds per
call of each metric plus the interpreter, platform and git commit it ran on:

```bash
python benchmarks/bench.py --save
python benchmarks/bench.py --save --samples 9
```

`--samples N` applies one sample count to every case; without it each case takes five.
The result schema and the committed results are described in
[`benchmarks/README.md`](https://github.com/jose-pr/objpath/blob/main/benchmarks/README.md).

Compare two runs on their medians, and only between runs from the same machine and
interpreter. A run on a developer machine is a sanity check; performance claims in release
notes come from CI runs.

## What It Covers

- `stat()` of an object, `iterdir()` and `walk()` of 72 entries
- `read_bytes()` and `write_bytes()` of 256 KiB, `mkdir()`, `rename()`, `unlink()` and a
  server-side `copy()`
- `rm(recursive=True)` of a 33-object tree, and the number of requests it sends
  (one `HeadObject`, one `ListObjectsV2` and one `DeleteObjects`)
- each of the above through `S3Path` (metric group `s3`) and through
  `URIPath("s3://...")` (group `s3-uri`), so the cost of the URI layer is the difference
  between the two groups

## Caveats

The bucket is held by `moto` in the benchmark's own process, with no network latency, so
the numbers show the overhead of the path layer and of moto, not how a store behaves over
a link. Timings depend on the machine, the interpreter and what else is running; compare
medians, as described above.
