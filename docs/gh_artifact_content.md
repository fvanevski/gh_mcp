# Bounded artifact-content inspection

`gh_list_artifact_files` and `gh_read_artifact_file` provide read-only inspection of files inside one exact GitHub Actions artifact without exposing a generic archive-download, extraction, deletion, or shell surface.

## Identity and retrieval

Both tools accept a positive exact `artifact_id`. Before downloading ZIP content, the server reads authoritative artifact metadata and rejects an expired artifact or an artifact whose reported archive size exceeds the server hard limit. Returned evidence preserves the artifact ID/name, reported artifact size and digest, expiry, workflow-run ID, and workflow head SHA.

The ZIP body is retrieved through the shared governed raw-byte evidence path. Downloaded bytes are bounded and fingerprinted with SHA-256. Response framing used internally to recover request/rate-limit metadata must never reach the ZIP evidence sink, and a body-visible raw-byte read is not transparently retried.

## Archive safety

Artifact archives are inspected in temporary server state and are never extracted into a caller-controlled filesystem tree. Inspection rejects:

- empty or NUL-containing paths;
- Unix absolute paths and Windows drive-qualified paths;
- `..` path traversal;
- duplicate or normalized-path conflicts;
- file/directory ancestor conflicts;
- encrypted entries;
- symbolic links;
- non-regular special entries;
- entry-count, archive-byte, aggregate-uncompressed-byte, or readable-file-byte hard-limit violations.

Temporary ZIP state is scoped to the operation and removed on both success and failure.

## `gh_list_artifact_files`

The listing returns normalized regular-file paths and compressed/uncompressed sizes using bounded pagination. `total_count`, `page`, `per_page`, `has_more`, `truncated`, and warning metadata must be preserved by callers. Directory entries are not returned as files.

A truncated page is not a complete artifact inventory. Callers must page until `has_more=false` before making claims that require complete file-list evidence.

## `gh_read_artifact_file`

The requested path must already be in exact normalized forward-slash form. The selected ZIP entry must be a regular file within the configured hard byte limit. The complete file is read under that hard limit before returned content is bounded by caller `max_bytes`.

Only strict UTF-8 text/JSON is accepted. Invalid UTF-8 or NUL-containing content is rejected rather than streamed as arbitrary binary data.

The result exposes complete-file identity/evidence, including total and returned byte counts, explicit truncation/warning state, and SHA-256 for the validated complete file. A truncated returned body therefore remains verifiable as a bounded projection of a known complete file but must not be represented as complete textual evidence.

## Non-goals

These tools do not expose artifact deletion, workflow-log deletion, arbitrary archive extraction, arbitrary binary streaming, arbitrary public `gh api`, or generic shell/subprocess execution. They do not weaken the request governor, write policy, or exact-state contracts used elsewhere in the server.

## Regression coverage

Focused tests cover exact metadata/archive routing, expiry, traversal and absolute paths, duplicate/conflicting entries, symlinks and special entries, encryption, archive/uncompressed/file hard bounds including equality boundaries, invalid UTF-8/NUL content, normalized requested paths, pagination/truncation, archive/file digests, raw-byte response metadata, and temporary-state cleanup.
