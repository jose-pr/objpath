# Paths and backends

The paths need no URI. Each store needs its own extra: `s3`, `gs` or `az`.

::: objpath
    options:
      show_if_no_docstring: true
      filters: ["!^_"]
      members:
        - S3Path
        - S3Backend
        - BaseS3Backend
        - GSPath
        - GSBackend
        - BaseGSBackend
        - AZPath
        - AZBackend
        - BaseAZBackend
