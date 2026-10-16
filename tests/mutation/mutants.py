"""The mutation list: one single-line change to a guard of the library per
entry, and the tests that must fail because of it.

`run.py` applies each entry to a temporary copy of `src/objpath`. `old`
is the exact text of the line (or lines) to replace and must occur once in
`file`, which is relative to `src/objpath`; when the code moves on and
the text goes, the entry is reported stale until it is re-expressed. `tests`
are pytest arguments (node ids, or a file when any of its tests may be the
one to catch the mutant) that all pass on the unmutated code. README.md says
how to add an entry.
"""

from __future__ import annotations

from typing import NamedTuple


class Mutant(NamedTuple):
    id: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...]
    note: str
    platform: str | None = None


MUTANTS: list[Mutant] = []


def mutant(
    id: str,
    file: str,
    old: str,
    new: str,
    tests,
    note: str,
    platform: str | None = None,
) -> None:
    """Add an entry to the list. `tests` is one pytest argument or a sequence;
    `platform` is the `os.name` its tests need ("nt"), or None for any."""
    if isinstance(tests, str):
        tests = (tests,)
    MUTANTS.append(Mutant(id, file, old, new, tuple(tests), note, platform))


# --- carried from the object-store schemes of pathapi ---------------------
mutant(
    "s3-exclusive-create-store-error",
    "_s3.py",
    "            if is_not_implemented(error):\n                return False\n            raise oserror(error, self) from None\n",
    "            if is_not_implemented(error):\n                return False\n            raise\n",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "open('x') lets the SDK's ClientError out untranslated",
)

mutant(
    "s3-exclusive-create-transport-error",
    "_s3.py",
    "        except TRANSPORT_ERRORS as error:\n            raise oserror(error, self) from None\n        return True\n\n    def _stat_root",
    "        except TRANSPORT_ERRORS as error:\n            raise\n        return True\n\n    def _stat_root",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "open('x') lets a broken connection out untranslated",
)

mutant(
    "s3-stat-bucket-error",
    "_s3.py",
    "            self._client.head_bucket(Bucket=self.bucket)\n        except S3_ERRORS as error:\n            raise oserror(error, self) from None\n",
    "            self._client.head_bucket(Bucket=self.bucket)\n        except S3_ERRORS as error:\n            raise\n",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "stat() of a bucket lets the SDK's error out untranslated",
)

mutant(
    "s3-checksum-store-error",
    "_s3.py",
    "            if is_not_found(error):\n                return None\n            raise oserror(error, self) from None\n",
    "            if is_not_found(error):\n                return None\n            raise\n",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "checksum() lets the SDK's ClientError out untranslated",
)

mutant(
    "s3-checksum-transport-error",
    "_s3.py",
    "        except TRANSPORT_ERRORS as error:\n            raise oserror(error, self) from None\n        digests = {}",
    "        except TRANSPORT_ERRORS as error:\n            raise\n        digests = {}",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "checksum() lets a broken connection out untranslated",
)

mutant(
    "s3-existence-check-error",
    "_s3.py",
    "            raise oserror(error, self) from None\n        return True\n\n    def _has_prefix",
    "            raise\n        return True\n\n    def _has_prefix",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "the existence check of a key lets the SDK's error out untranslated",
)

mutant(
    "s3-first-keys-error",
    "_s3.py",
    "                Bucket=self.bucket, Prefix=prefix, MaxKeys=limit\n            )\n        except S3_ERRORS as error:\n            raise oserror(error, self) from None\n",
    "                Bucket=self.bucket, Prefix=prefix, MaxKeys=limit\n            )\n        except S3_ERRORS as error:\n            raise\n",
    "tests/test_s3_errors.py::test_a_listing_that_fails_while_removing_a_directory_keeps_it",
    "rmdir() lets a failed listing of the directory's keys out untranslated",
)

mutant(
    "s3-marker-write-error",
    "_s3.py",
    '                self._client.put_object(Bucket=self.bucket, Key=key, Body=b"")\n            except S3_ERRORS as error:\n                raise oserror(error, self) from None\n',
    '                self._client.put_object(Bucket=self.bucket, Key=key, Body=b"")\n            except S3_ERRORS as error:\n                raise\n',
    "tests/test_s3_errors.py::test_a_marker_write_that_fails_after_conditional_writes_are_refused_is_translated",
    "mkdir() lets the failed marker write out untranslated",
)

mutant(
    "s3-delete-object-error",
    "_s3.py",
    "            self._client.delete_object(Bucket=self.bucket, Key=key)\n        except S3_ERRORS as error:\n            raise oserror(error, self) from None\n",
    "            self._client.delete_object(Bucket=self.bucket, Key=key)\n        except S3_ERRORS as error:\n            raise\n",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "unlink() lets the SDK's error out untranslated",
)

mutant(
    "s3-batch-delete-declined-error-swallowed",
    "_s3.py",
    "                if not on_error(translated):\n                    raise translated from None\n                continue\n",
    "                if not on_error(translated):\n                    pass\n                continue\n",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "a failed batch delete that the caller did not tolerate is skipped",
)

mutant(
    "s3-batch-delete-error-not-offered",
    "_s3.py",
    "                translated = oserror(error, self)\n                if not on_error(translated):",
    "                translated = oserror(error, self)\n                if True:",
    "tests/test_s3_errors.py::test_a_batch_delete_that_fails_is_offered_translated_and_the_walk_goes_on",
    "a failed batch delete is raised although the caller tolerates it",
)

mutant(
    "s3-rename-source-error",
    "_s3.py",
    "                return None\n            raise oserror(error, self) from None\n\n    def _rename_copy",
    "                return None\n            raise\n\n    def _rename_copy",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "rename() lets the SDK's error for the source's HEAD out untranslated",
)

mutant(
    "s3-rename-copy-error",
    "_s3.py",
    "            client.delete_object(Bucket=self.bucket, Key=self.key)\n        except S3_ERRORS as error:\n            raise oserror(error, self) from None\n",
    "            client.delete_object(Bucket=self.bucket, Key=self.key)\n        except S3_ERRORS as error:\n            raise\n",
    "tests/test_s3_errors.py::test_a_failed_request_is_an_oserror_naming_the_path_and_changes_nothing",
    "rename() lets the SDK's error for the copy out untranslated",
)

mutant(
    "store-recursive-rm-existence-error-reads-as-missing",
    "_storepath.py",
    "            if not on_error(error):\n                raise\n            return\n\n        if not keys:\n            marker",
    "            pass\n\n        if not keys:\n            marker",
    "tests/test_s3_errors.py::test_a_failed_existence_check_stops_a_recursive_remove_before_any_delete",
    "rm(recursive=True) reads a failed existence check as 'no such object' and deletes the prefix tree",
)

mutant(
    "store-bucket-root-is-removed-recursively",
    "_storepath.py",
    '        if not self.key:\n            error = PermissionError(f"recursive {self._TOP} delete is not enabled")',
    '        if False:\n            error = PermissionError(f"recursive {self._TOP} delete is not enabled")',
    "tests/test_s3.py::test_rm_recursive_root_guard",
    "rm(recursive=True) of a bucket root empties the bucket",
)

mutant(
    "s3-mtime-is-zero",
    "_s3.py",
    'st_mtime=int(head["LastModified"].timestamp()),',
    "st_mtime=0,",
    "tests/test_s3.py::test_stat_and_listing_report_the_last_modified_time_as_an_epoch",
    "S3Path.stat().st_mtime is always 0",
)

mutant(
    "store-recursive-copy-of-unreachable-keys",
    "_keys.py",
    '    unreachable = path._unreachable_keys()\n    if unreachable:\n        raise refusal(path, "copy", unreachable)',
    '    unreachable = path._unreachable_keys()\n    if False:\n        raise refusal(path, "copy", unreachable)',
    "tests/test_s3.py::test_copy_recursive_of_such_a_prefix_is_refused_before_anything_is_written",
    "a recursive copy of a prefix that holds keys no walk reaches goes on and leaves them behind",
)

mutant(
    "store-recursive-remove-of-unreachable-keys",
    "_storepath.py",
    '            if unreachable:\n                error = refusal(self, "remove", unreachable)',
    '            if False:\n                error = refusal(self, "remove", unreachable)',
    "tests/test_s3.py::test_rm_recursive_of_such_a_prefix_removes_nothing",
    "rm(recursive=True) of a prefix that holds keys no walk reaches removes them",
)

mutant(
    "store-upload-stream-uploads-after-a-discard",
    "_writer.py",
    "        self._dropped = True\n        self._file.close()\n",
    "        pass\n",
    (
        "tests/test_s3.py::test_a_write_block_that_raised_sends_no_upload_moto",
        "tests/test_gs_fake.py::test_a_write_block_that_raised_uploads_nothing",
        "tests/test_az_fake.py::test_a_write_block_that_raised_uploads_nothing",
    ),
    "an S3Path, GSPath or AZPath write whose with block raised is uploaded on close",
)

mutant(
    "store-upload-stream-puts-iobase-ahead-of-the-mixin",
    "_writer.py",
    "class SpoolUploadStream(_providers.DiscardOnError, _io.BufferedIOBase):\n",
    "class SpoolUploadStream(_io.BufferedIOBase, _providers.DiscardOnError):\n",
    (
        "tests/test_s3.py::test_a_write_block_that_raised_sends_no_upload_moto",
        "tests/test_gs_fake.py::test_a_write_block_that_raised_uploads_nothing",
        "tests/test_az_fake.py::test_a_write_block_that_raised_uploads_nothing",
    ),
    "an S3Path, GSPath or AZPath write whose with block raised is uploaded by the __exit__ of IOBase",
)


# --- construction, identity and text of a path ------------------------------------

mutant(
    "store-place-is-required",
    "_places.py",
    '        if donor is None:\n            raise TypeError(f"{owner} needs {kind}= (or a segment that is an {owner})")\n',
    '        if False:\n            raise TypeError(f"{owner} needs {kind}= (or a segment that is an {owner})")\n',
    "tests/test_s3_path.py::test_a_path_without_a_bucket_is_a_type_error",
    "a path with no bucket is built",
)

mutant(
    "store-place-may-hold-a-slash",
    "_places.py",
    '    if not value or "/" in value:\n',
    "    if not value:\n",
    "tests/test_s3_path.py::test_the_bucket_must_be_a_name_with_no_slash",
    "a bucket name with a slash is accepted",
)

mutant(
    "s3-backend-and-options-are-both-accepted",
    "_s3.py",
    "        if backend is not None and client_kwargs:\n",
    "        if False:\n",
    "tests/test_s3_path.py::test_backend_together_with_client_options_is_a_type_error",
    "backend= and client options are both taken, the options silently dropped",
)

mutant(
    "store-equality-ignores-the-bucket",
    "_storepath.py",
    "        return (self._place, self.key)\n",
    "        return (self.key,)\n",
    "tests/test_s3_path.py::test_the_same_key_in_two_buckets_is_two_paths",
    "the same key in two buckets is one path",
)

mutant(
    "az-equality-ignores-the-account",
    "_az.py",
    '        return (self.account or "", self._place, self.key)\n',
    "        return (self._place, self.key)\n",
    "tests/test_az_path.py::test_paths_of_two_accounts_with_one_container_and_key_are_not_equal",
    "the same container and key of two accounts is one path",
)

mutant(
    "store-key-keeps-the-trailing-slash",
    "_keys.py",
    '    return key[:-1] if key.endswith("/") else key\n',
    "    return key\n",
    "tests/test_s3_path.py::test_key_rules",
    "dir/ names another key than dir",
)

mutant(
    "store-key-keeps-the-leading-slash",
    "_keys.py",
    '    key = path.lstrip("/")\n',
    "    key = path\n",
    "tests/test_s3_path.py::test_the_segments_of_a_path_are_its_names_rooted_or_not",
    "/dir names another key than dir",
)

mutant(
    "store-children-lose-the-listing-stat",
    "_storepath.py",
    "        child._hint = stat_hint\n",
    "        child._hint = None\n",
    "tests/test_gs_fake.py::test_a_listed_child_answers_one_stat_from_its_listing_and_the_next_asks_the_store",
    "a listed child asks the store for the stat its listing gave",
)

mutant(
    "store-pickle-carries-any-backend",
    "_storepath.py",
    '        if getattr(self._backend, "picklable", False):\n',
    "        if True:\n",
    "tests/test_s3_path.py::test_a_pickled_path_leaves_a_backend_that_does_not_say_so",
    "a pickled path carries the options and credentials of its backend",
)

mutant(
    "az-account-is-not-learned-from-the-connection-string",
    "_az_backend.py",
    '            if name.strip().lower() == "accountname" and value:\n',
    "            if False:\n",
    "tests/test_az_path.py::test_the_account_is_learned_from_the_connection_string_or_account_url",
    "a backend of a connection string names no account",
)

mutant(
    "store-is-local",
    "_storepath.py",
    '        both sides of a sync as non-local."""\n        return False\n',
    '        both sides of a sync as non-local."""\n        return True\n',
    "tests/test_az_path.py::test_a_core_path_is_not_local",
    "a bucket is read as this machine",
)

# --- what is the same store, and what a backend does ------------------------------

mutant(
    "store-same-filesystem-ignores-the-backend",
    "_storepath.py",
    "        return self._place == other._place and self._backend is other._backend\n",
    "        return self._place == other._place\n",
    "tests/test_gs_fake.py::test_rename_onto_another_backend_is_not_implemented_and_move_streams",
    "rename() reaches a bucket through the wrong client",
)

mutant(
    "store-copy-across-backends",
    "_storepath.py",
    "            and self._backend is target._backend\n        )",
    "            and True\n        )",
    "tests/test_gs_fake.py::test_copy_between_two_supplied_backends_streams",
    "a copy between two backends is one request of the store's",
)

mutant(
    "az-copy-across-containers",
    "_az.py",
    "        return super()._store_copy_possible(target) and self._place == target._place\n",
    "        return super()._store_copy_possible(target)\n",
    "tests/test_az_fake.py",
    "a copy to another container is one service copy",
)

mutant(
    "store-rename-onto-another-class",
    "_storepath.py",
    "        elif type(target) is not type(self) or not self._same_filesystem(target):\n",
    "        elif False:\n",
    "tests/test_gs_fake.py::test_rename_onto_another_backend_is_not_implemented_and_move_streams",
    "rename() accepts a target it cannot reach",
)

mutant(
    "uri-copy-across-backends",
    "_uri_store.py",
    "            and self._supplied_backend() is target._supplied_backend()\n",
    "            and True\n",
    "tests/test_uri_s3.py::test_copy_between_two_supplied_backends_is_read_and_written",
    "a copy between two supplied backends is one request of the store's",
)

mutant(
    "uri-rename-across-backends",
    "_uri_store.py",
    "        if isinstance(target, ObjectStoreURIPath) and not self._same_filesystem(target):\n",
    "        if False:\n",
    "tests/test_gs_fake.py::test_rename_onto_another_backend_is_not_implemented_and_move_streams",
    "a URI path renames on the backend of its source and ignores the target's",
)

mutant(
    "uri-callback-gets-the-core-path",
    "_uri_store.py",
    "                return ignore_error(\n                    error, self if path is None else self._from_core(path)\n                )\n",
    "                return ignore_error(\n                    error, self if path is None else path\n                )\n",
    "tests/test_uri_s3.py::test_rm_offers_the_uri_path_of_each_key_the_store_refuses",
    "rm() offers the path of the core class to the caller's callback",
)

mutant(
    "client-close-keeps-the-client",
    "_clients.py",
    "            client, self._client = self._client, None\n",
    "            client = self._client\n",
    "tests/test_backends.py::test_closing_a_backend_closes_its_client_and_the_next_use_builds_another",
    "close() leaves the closed client to be used again",
)

mutant(
    "exceptions-changed-error-is-not-an-oserror",
    "exceptions.py",
    "class ObjectChangedError(ObjPathError, OSError):\n",
    "class ObjectChangedError(ObjPathError):\n",
    "tests/test_az.py::test_az_a_read_across_an_overwrite_raises_and_returns_no_mixed_bytes",
    "a stream whose object changed raises something that is not an OSError",
)

mutant(
    "unclosed-spool-is-uploaded-at-collection",
    "_writer.py",
    "        return self._dirty and not self._dropped\n",
    "        return False\n",
    "tests/test_spool_unclosed.py::test_an_unclosed_written_stream_is_discarded_at_collection",
    "a spooled stream that the collector finalizes unclosed uploads what it holds",
)
mutant(
    "unmodified-spool-warns-at-collection",
    "_writer.py",
    "        return self._dirty and not self._dropped\n",
    "        return True\n",
    "tests/test_spool_unclosed.py::test_an_unclosed_stream_that_was_not_modified_is_collected_silently",
    "a spooled stream with nothing to upload warns when collected",
)

mutant(
    "objpath-store-rm-always-passes-max-workers",
    "_storepath.py",
    '                # Only when given: pathapi 0.1.1 has no such keyword.\n                **({} if max_workers is None else {"max_workers": max_workers}),\n            )\n\n        def on_error(',
    "                max_workers=max_workers,\n            )\n\n        def on_error(",
    "tests/test_az_fake.py::test_the_generic_calls_get_max_workers_only_when_it_is_given",
    "rm() hands pathapi a max_workers it was not given, which a release without the keyword refuses",
)

mutant(
    "objpath-store-copy-always-passes-max-workers",
    "_storepath.py",
    '                # Only when given: pathapi 0.1.1 has no such keyword.\n                **({} if max_workers is None else {"max_workers": max_workers}),\n            )\n\n    def _store_copy_possible',
    "                max_workers=max_workers,\n            )\n\n    def _store_copy_possible",
    "tests/test_az_fake.py::test_the_generic_calls_get_max_workers_only_when_it_is_given",
    "copy() hands pathapi a max_workers it was not given, which a release without the keyword refuses",
)

mutant(
    "objpath-uri-copy-always-passes-max-workers",
    "_uri_store.py",
    '                # Only when given: pathapi 0.1.1 has no such keyword.\n                **({} if max_workers is None else {"max_workers": max_workers}),\n',
    "                max_workers=max_workers,\n",
    "tests/test_az_fake.py::test_the_generic_calls_get_max_workers_only_when_it_is_given",
    "the URI copy() hands pathapi a max_workers it was not given",
)
