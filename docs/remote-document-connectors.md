# Remote document connectors

Google Drive and S3-compatible object sources can discover remote metadata and,
when their **server-side profile** enables it, extract bounded document passages
for agent curation and human review. Connection config cannot enable retention,
supply credentials, change provider endpoints, or execute source instructions.
The default remains metadata-only discovery.

## Google Drive folder

Install the optional authentication dependency:

```sh
uv sync --extra drive
```

For the container, build with `--build-arg KNOWLEDGE_RUNTIME_EXTRAS="drive"`
(or a space-separated list of selected extras). The local Compose build also accepts
`KNOWLEDGE_RUNTIME_EXTRAS="drive objects"` in its environment. Provision a service-account credential on the server and
give that account read access to the selected folder. Enable the Drive API for
the credential's project. A real account and its IAM/Drive sharing settings must
be qualified separately from the offline tests.

Store this JSON under a server source reference such as
`KNOWLEDGE_SOURCE_DRIVE_FILE=/run/secrets/drive-profile.json`. Secret files must
be regular, owner-only files, at most 64 KiB. The profile is not submitted by
the agent or saved as connection config.

```json
{
  "credentials": {
    "type": "service_account",
    "project_id": "your-project",
    "private_key_id": "your-key-id",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
    "client_email": "reader@your-project.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token"
  },
  "folders": ["YOUR_FOLDER_ID"],
  "retain_document_content": false
}
```

The usual additional fields in a Google service-account JSON file are accepted,
including `client_id`, `auth_uri`, certificate URLs, and `universe_domain`.
This connector supports the public Google API universe and signed service-account
authentication. It does not use application default credentials or impersonate
a Workspace user. The configured token endpoint is fixed to Google's endpoint.

Create a connection with kind `google_drive` and this config:

```json
{
  "secret_ref": "KNOWLEDGE_SOURCE_DRIVE",
  "folder_id": "YOUR_FOLDER_ID",
  "max_files": 100
}
```

The selected folder must appear in the operator profile's `folders` allowlist.
OIDC source policy also pins both `secret_ref` and `folder_id`; a new folder needs
its own approval and grant. Profiling limits can change without changing that
identity.

Discovery covers the folder's direct children visible to the source credential.
Shared Drive folders use the exact returned Drive ID and a `drive` corpus;
ordinary folders use the `user` corpus. Subfolders and shortcuts are metadata
assets; the connector does not follow them. Connect approved subfolders separately.
Drive permissions are not mirrored into knowledge-layer permissions.

The metadata path uses fixed Drive v3 `files.get` and `files.list` resources and
requests a metadata-only read scope. Set `retain_document_content` to `true` in
the **operator profile** to authorize fixed downloads and a read-only content
scope. Google Docs export as `text/plain`; uploaded text, Markdown, PDF, DOCX and
HTML use bounded extraction. Sheets, Slides, images, encrypted documents and
unsupported content retain metadata or report extraction gaps. HTML extraction
does not execute scripts or fetch links. PDF extraction does not perform OCR.

The connector checks the exact file ID, parent folder and monotonic Drive version
before and after every content read. A changed version or scope fails discovery,
so it cannot replace a prior snapshot with mixed-revision evidence. Downloaded
binary content is checked against Drive's MD5 checksum when available; retained
evidence also records a SHA-256 digest. Stable asset identity uses the file ID;
revision fingerprints include native metadata and the content digest.

Google Docs locators explicitly refer to lines in the `text/plain` export and
include the Drive file ID and version. They do not claim a Google Docs editor
page or line coordinate. Uploaded PDFs preserve PDF page and extracted-line
locators. Passage evidence remains untrusted source text until reviewed in the
[agent build flow](agent-build-flow.md).

## S3-compatible documents

Install `uv sync --extra objects`. Existing bucket profiles and prefix-scoped
connections keep metadata-only behavior. To authorize extraction, the operator
can add these fields to the server bucket profile:

```json
{
  "bucket": "your-documents",
  "region": "us-east-1",
  "retain_document_content": true,
  "max_file_bytes": 16777216,
  "max_total_bytes": 67108864
}
```

Existing server-controlled endpoint and credential fields remain supported.
The connection still contains only `secret_ref`, `prefix`, and `max_objects`.
Source policy pins the bucket profile reference and prefix. The operator's
content flag applies to supported documents within those approved prefixes;
use separate profiles when metadata and document-reading permissions differ.

Supported text, Markdown, PDF, DOCX and HTML objects use the same extraction
limits and citation shapes as local files. CSV, JSON, Parquet and other table
objects remain metadata assets. Reads require a visible ETag and use fixed
`GetObject` calls with `IfMatch`. Returned ETag, content length and modification
time must agree with the listing, and the full bounded body must match its
declared length. A failed precondition fails discovery. Access failures and
unsupported or oversized documents retain explicit metadata/extraction gaps.
Object streams and clients close after discovery.

## Bounds and evidence limits

Drive allows at most 100 children and 20 listing pages. S3 retains its existing
single complete listing and limit of 500 objects. Incomplete listings and repeated
continuation tokens fail discovery rather than implying removed assets.

Operator profiles can lower document budgets: at most 16 MiB per downloaded file,
64 MiB total downloaded content, and 8 MiB aggregate metadata/serialized result.
The checked discovery deadline is 60 seconds, with at most 10 seconds per request
and 5 seconds for connection establishment. S3 uses one total SDK request attempt.
A current request or parser operation may complete before the next deadline check;
the deadline is not a process-level cancellation guarantee. Existing local PDF
decompression, operation, text and page budgets also apply.

The registry stores at most 50 passages of 280 characters per document, plus
bounded heading and quantified-claim candidates. It does not retain whole remote
files. Extraction and retention completeness are reported separately; empty,
encrypted, malformed, oversized, truncated and OCR-dependent documents never
become a claim of complete content coverage. Folder listing completeness is
credential-visible observation, not an atomic provider-wide snapshot.

Offline tests exercise actual request construction with mocked HTTP/S3 transports,
signed Google assertions against a mocked token endpoint, revision drift,
content denial, extraction locators, retention restrictions and budget failures:

```sh
uv run --no-sync --with google-auth python -m pytest -q \
  tests/test_remote_documents.py tests/test_knowledge_connectors_extended.py \
  tests/test_source_policy.py
```

No real Google account, Drive folder, S3 bucket, IAM policy, production network,
large customer corpus or OCR behavior has been verified by those tests. Provider
API behavior follows Google's [files.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list),
[files.get](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/get)
and [files.export](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export)
contracts; Google exports have their own 10 MB service limit.
