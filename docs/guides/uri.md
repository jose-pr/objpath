# The `s3:`, `gs:` and `az:` schemes

With [`uripath`](https://jose-pr.github.io/uripath/) installed (`pip install
"objpath[uri]"`), the URL of an object is a path:

<!-- example: skip: connects to a store -->
```python
from uripath import URIPath

URIPath("s3://data/reports/2026.csv")          # an S3URIPath
URIPath("gs://ml/models/latest")               # a GSURIPath
URIPath("az://mycompany/ops/logs/app.log")     # an AZURIPath
```

`uripath` finds the classes through the entry points of the group `uripath.schemes` that
this distribution declares; nothing else is needed, and `import objpath` never imports
`uripath`. Each class is a URI path (see the `uripath` documentation for everything about
URIs) whose requests are done by the matching path of this package, so the two behave
alike.

| Scheme | Class | Authority | First segment |
| --- | --- | --- | --- |
| `s3:` | `S3URIPath` | the bucket | the key |
| `gs:` | `GSURIPath` | the bucket | the key |
| `az:` | `AZURIPath` | the storage account | the container, then the key |

`backend=` is a `BaseS3Backend`, `BaseGSBackend` or `BaseAZBackend` used as it is by the path
it is given to, whatever bucket the URI names, and by the paths derived from it on the same
bucket. Without it, the default backend of the process (of the
account, for `az:`) is used. `az://account` is a directory whose children are the containers.

Two URI paths of one store reached by one client copy server side, also between buckets
(`az:` within a container); `rename()` stays within a bucket, and across buckets raises
`NotImplementedError` so `move()` copies. The callbacks of `rm()` and `copy()` get URI
paths.
