# TeleDrive

**Telegram personal backup uploader** — Docker Web UI, Windows/Linux desktop app and an automation CLI. This fork adds multi-account scheduling and SQLAlchemy-backed persistent state.

> Telegram Saved Messages and ordinary Telegram cloud chats are **not end-to-end encrypted**. Encrypt sensitive files before uploading when confidentiality from Telegram is required.

## Quick start: Docker Web UI

The server/NAS deployment has a browser dashboard, upload queue, history, account management, Telegram login prompts and logs.

~~~bash
git clone -b feature/docker-webui-multiaccount https://github.com/babico/TeleDrive.git
cd TeleDrive
mkdir -p config data uploads
cp .env.example .env
cp examples/config.docker.yaml config/config.yaml
chmod 600 .env config/config.yaml
~~~

Edit **.env** and set a unique **TELEDRIVE_WEB_PASSWORD** (minimum 16 characters). Set **PUID** and **PGID** to the host user owning the mounted folders if they differ from 1000. Then:

~~~bash
docker compose up -d --build
docker compose logs -f teledrive
~~~

Open **http://127.0.0.1:8080** on the Docker host. The port binds to loopback by default. For remote administration use an SSH tunnel, VPN or HTTPS reverse proxy. Never expose HTTP Basic authentication over untrusted plaintext HTTP.

In **Settings**, enter the Telegram API ID/hash from [my.telegram.org](https://my.telegram.org/apps), each enabled account's phone number and upload target, then select a strategy. Choose **Run batch**; the browser prompts for login codes and 2FA passwords as needed.

Docker mounts: **./config → /config** (settings), **./data → /data** (sessions, auth handoff, default SQLite DB), and **./uploads → /uploads:ro** (source files). To upload a different host directory, change only the host-side path of the **/uploads** bind mount.

The Docker config example is [examples/config.docker.yaml](examples/config.docker.yaml). No desktop environment, X11 or PySide6 is needed inside Docker.

## OpenAPI / Web API reference

The Docker Web UI publishes **authenticated interactive API documentation** at:

- **/docs** — Swagger UI
- **/redoc** — ReDoc
- **/openapi.json** — OpenAPI JSON specification

Example (local host):

~~~bash
curl -u admin:YOUR_PASSWORD http://127.0.0.1:8080/openapi.json
~~~

These endpoints require the same HTTP Basic credentials as the administration UI. On requests that mutate state (`POST`, `PUT`), also send `X-TeleDrive-CSRF: 1`. The API should be accessed over an HTTPS reverse proxy or VPN when remote.

## Multi-account strategies

| Strategy | Behavior |
| --- | --- |
| **single** | Use the first enabled Telegram account |
| **round_robin** | Rotate the preferred account between successive files, with fallback |
| **failover** | Try accounts in configured priority order |
| **parallel** | Concurrent account-affine workers, with fallback |

Each account has a separate session file and optional API/target overrides. FloodWait and cooldown state are account-specific, and upload history records the sending account. Disabled accounts remain in settings without connecting.

**Saved Messages caution:** the target **me** points to a different private Saved Messages area for every account. If you want one destination, use a shared private channel and grant posting access to all accounts.

Telegram limits still apply. Oversized files can be split into lossless parts before uploading.

## Database: synchronous SQLAlchemy dialects

Configure **state.database_url** in YAML or the Web UI. SQLite works without a database server; PostgreSQL and MariaDB/MySQL drivers are included in the Web image. Other synchronous SQLAlchemy dialects can be used when the dialect, DBAPI driver and any required OS libraries are installed. There is no hardcoded database vendor whitelist, but not every third-party dialect is necessarily tested or compatible.

~~~yaml
state:
  database_url: "sqlite:////data/state.db"
~~~

Other database URL examples:

~~~text
postgresql+psycopg://user:password@db:5432/teledrive
mysql+pymysql://user:password@db:3306/teledrive
oracle+oracledb://user:password@db:1521/?service_name=FREEPDB1
mssql+pyodbc://user:password@db/teledrive?driver=ODBC+Driver+18+for+SQL+Server
~~~

To install another trusted dialect/driver at Docker image build time, set **TELEDRIVE_DB_EXTRA_PACKAGES** in **.env**, then rebuild:

~~~dotenv
TELEDRIVE_DB_EXTRA_PACKAGES=oracledb
~~~

~~~bash
docker compose build --no-cache
docker compose up -d
~~~

Some drivers, especially ODBC variants, also require system libraries in a derived Docker image. For remote databases, use TLS and grant only the permissions TeleDrive needs.

## Desktop app (Windows/Linux)

A PySide6 desktop UI remains available, including multiple Telegram accounts, strategy selection, database URL settings, account-scoped login/2FA and SQLAlchemy-backed queue/history. The desktop example is [examples/config.desktop.yaml](examples/config.desktop.yaml); the application creates its own configuration under the user's application data directory.

~~~bash
python -m pip install -r requirements.txt
python -m app.main
~~~

Desktop data lives in **%LOCALAPPDATA%/Telegram Auto Upload** on Windows or **~/.local/share/Telegram Auto Upload** on Linux (unless **XDG_DATA_HOME** is set).

Build helpers are in **scripts/**:

~~~powershell
# Windows: portable executable, then Inno Setup installer
./scripts/build.ps1
./scripts/build-installer.ps1
~~~

~~~bash
# Linux: PyInstaller package in dist-installer/
bash scripts/build-linux.sh
~~~

The release workflow uses these scripts for version tags. Upstream's existing binary releases do **not** necessarily include this fork's Docker or multi-account features.

### Desktop screenshots

![Desktop dashboard](website/public/images/dashboard.png)
![Desktop upload queue](website/public/images/upload-queue.png)
![Desktop settings](website/public/images/settings.png)

## CLI and automation

The scan/upload backend also runs without a UI. Copy the desktop example to a writable config and edit its source path and Telegram credentials first.

~~~bash
cp examples/config.desktop.yaml local-config.yaml
python -m teledrive.uploader --config local-config.yaml --scan-only
python -m teledrive.uploader --config local-config.yaml --run-once
~~~

The Web UI's automatic batch scheduler is configured in **Settings → Automation**. Avoid running multiple client processes against the same Telegram session; state leases alone cannot make shared session files safe.

## Telegram setup

1. Log into [my.telegram.org](https://my.telegram.org) with your normal Telegram account, then create credentials under **API development tools**. No BotFather token is needed.
2. Set the destination to **me** (your Saved Messages), a public **@channel**, or the numeric chat ID for a channel/group to which the account can post.
3. Authorize each account with its Telegram login code and, if required, 2FA password.
4. Scan a small folder and test one batch before enabling scheduling.

Keep source files stable during uploads; back up your Telethon sessions and state DB before migrating hosts.

## Security

The Web UI is password-protected by default. Container defaults include loopback binding, non-root execution, dropped Linux capabilities, a read-only root filesystem and read-only upload source. Telegram sessions and auth handoff files have restrictive permissions; distributed leases and per-file claims help prevent duplicate uploads.

See [SECURITY.md](SECURITY.md) for the threat model, TLS/host allowlists, secret-file authentication and database-driver trust. Default options are documented in [.env.example](.env.example) and [docker-compose.yml](docker-compose.yml).

## Website and development

The Astro marketing website under **website/** is separate from the Docker administration UI:

~~~bash
cd website
npm ci
npm run dev
npm run build
~~~

| Path | Purpose |
| --- | --- |
| **teledrive/uploader.py** | Telegram engine, scanning, account strategies |
| **teledrive/db.py** | SQLAlchemy state, migrations, leases and claims |
| **teledrive/desktop.py** | Desktop executable launcher and frozen CLI dispatcher |
| **web/** | FastAPI admin Web UI and static assets |
| **app/** | PySide6 desktop application |
| **scripts/** | Windows/Linux desktop packaging scripts |
| **examples/** | Docker and desktop sample config files |
| **tests/** | Regression/security tests |
| **website/** | Standalone Astro marketing site |
| **installer/** | Windows Inno Setup assets |

Development checks:

~~~bash
python -m pip install -r requirements-web.txt pytest
python -m pytest -q
python -m compileall -q teledrive web app tests
docker compose config --quiet
~~~

Security CI also builds the image, exercises the live Web API/authentication, and performs dependency and image scans.

## REST API and credential management — incremental implementation

TeleDrive now includes a database-backed **API Management** page in the Docker
WebUI and Desktop UI. An administrator can create scoped Bearer API keys, list
metadata, rotate and revoke them. Only SHA-256 token digests are persisted.
Full secrets are shown **once**. The same metadata DB makes tokens available
to both interfaces, provided they point at the same database.

Implemented REST v1 endpoints:

- `GET/POST /api/v1/admin/keys`, `POST /api/v1/admin/keys/{id}/rotate`,
  `DELETE /api/v1/admin/keys/{id}` (Basic + CSRF, WebUI administrators).
- `GET/POST/DELETE /api/v1/buckets` (scoped Bearer tokens).
- `GET /api/v1/buckets/{bucket}/objects`, `DELETE` on the same path
  with a `key` query parameter (metadata list and logical unlink).
- `PUT /api/v1/buckets/{bucket}/content?key=...` with raw
  `application/octet-stream`: streams to bounded temporary staging and
  uploads to **Telegram Bot API**, then commits the object manifest/current
  pointer before returning HTTP 201.
- `GET /api/v1/buckets/{bucket}/content?key=...`: streams checksum-verified
  Telegram object chunks with HTTP Range support.

Example upload after generating a key with `objects:write` permission:

~~~bash
curl -f -X PUT 'http://127.0.0.1:8080/api/v1/buckets/backups/content?key=folder%2Ffile.bin' \
  -H "Authorization: Bearer $TELEDRIVE_API_KEY" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary '@file.bin'
~~~

**Initial transport requirement:** REST binary PUT/GET currently needs a
Telegram Bot API account with administrator/post rights in a **private Telegram
channel**. Configure `TELEDRIVE_STORAGE_BOT_TOKEN` and
`TELEDRIVE_STORAGE_BOT_CHAT_ID` in the private local `.env` file. The official
Telegram-hosted Bot API limits file downloads to 20 MiB; the storage engine
uses 16 MiB Telegram chunks to work within that limit. The default REST
maximum upload is 2 GiB, configurable with
`TELEDRIVE_REST_MAX_UPLOAD_BYTES`. No S3 server or S3-primary storage is
involved. The original multi-user-account folder uploader remains available.

**Not yet supported by REST transfer:** direct Telethon user-account sessions,
generic asynchronous job tracking/resume, full multipart upload, presigned
URLs, or production S3 SigV4 endpoints. API keys are not AWS S3 credentials.
Object unlink retains immutable Telegram manifests; physical deletion waits
for safely verified garbage collection. API tokens with unused future scopes
do not enable those features until their endpoints are implemented.

Only expose the WebUI/API over trusted HTTPS or a VPN. The new binary route is
subject to a configured byte quota and requires an explicit Bearer token;
admin key issuance requires HTTP Basic authentication plus CSRF protection.

## REST object metadata (current implementation)

The authenticated REST API provides a metadata-only `HEAD` operation:

~~~bash
curl -I -H "Authorization: Bearer <API_KEY>" \
  "http://127.0.0.1:8080/api/v1/buckets/my-bucket/content?key=folder%2Ffile.bin"
~~~

It returns `Content-Length`, SHA-256-derived `ETag`, `Accept-Ranges`,
and `X-TeleDrive-Object-ID` without downloading bytes from Telegram.
Keys need the `objects:read` scope. This is REST functionality, **not yet
a complete AWS S3 HEAD Object implementation**.

## S3-compatible gateway — initial Telegram-backed core

**This is an early core subset, not full AWS S3 compatibility.** Bucket and
object payloads reside in Telegram; SQLAlchemy stores only object metadata,
encrypted S3 credentials and chunk/file IDs. No MinIO or external S3 backend
is used. The gateway is a **separate Docker Compose profile** and is disabled
by default.

### First-time S3 setup

1. Configure `TELEDRIVE_STORAGE_BOT_TOKEN` and
   `TELEDRIVE_STORAGE_BOT_CHAT_ID` in your local `.env`, following the
   Telegram Bot storage instructions. The bot must be an administrator in the
   dedicated private Telegram channel.
2. Generate a persistent S3 encryption master key on the Docker host:

~~~bash
mkdir -p config data
python - <<'PY'
from pathlib import Path
from cryptography.fernet import Fernet
p = Path("config/s3-master.key")
if p.exists():
    raise SystemExit("Refusing to overwrite existing master key")
with p.open("xb") as stream:
    stream.write(Fernet.generate_key() + b"\n")
p.chmod(0o600)
PY
~~~

Install `cryptography` locally if needed: `python -m pip install cryptography`.
Ensure the S3 container's `PUID`/`PGID` can read this file. **Back up the
master key securely**; losing it makes all issued S3 secrets unusable, and
replacing it does not restore old credentials.

3. Start both management UI and the opt-in S3 gateway:

~~~bash
docker compose --profile s3 up -d --build
~~~

The gateway binds **127.0.0.1:9000** by default. For remote clients, put
it behind an HTTPS reverse proxy or private VPN. Do not expose plaintext
HTTP or the Telegram bot token to the internet.

4. Open **WebUI → API Management → S3-compatible gateway** or the Desktop
**S3 Management** page. Issue an S3 Access Key ID and Secret Access Key,
selecting only the required permissions. The secret is displayed once,
encrypted in the DB and revocable immediately.

On Desktop, set `TELEDRIVE_S3_MASTER_KEY_FILE` to the path of the **same**
master key used by the Docker instance when sharing the same database.
The app will refuse S3 key creation if the master key is missing. Desktop
does not silently generate a different encryption key.

Example using boto3 (simple path-style, signed-payload requests):

~~~python
import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url="http://127.0.0.1:9000",
    aws_access_key_id="YOUR_ACCESS_KEY_ID",
    aws_secret_access_key="YOUR_SECRET_ACCESS_KEY",
    region_name="us-east-1",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "path", "payload_signing_enabled": True},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
)
s3.create_bucket(Bucket="backup-bucket")
s3.put_object(Bucket="backup-bucket", Key="test.txt", Body=b"Hello Telegram")
assert s3.get_object(Bucket="backup-bucket", Key="test.txt")["Body"].read() == b"Hello Telegram"
~~~

**Implemented now:** SigV4 header authentication (15-minute timestamp window),
individually scoped and encrypted access keys, ListBuckets, CreateBucket,
HeadBucket, GetBucketLocation, ListObjectsV1 (marker) and ListObjectsV2
(with delimiter/CommonPrefixes, signed V2 continuation tokens, and URL-encoded
list responses), PutObject, GetObject, HeadObject,
DeleteObject (logical unlink), DeleteBucket (only physically empty),
and single byte-range GET. Object writes are acknowledged only after Telegram
upload and manifest commit.

**Legacy S3 listing compatibility:** ListObjectsV1 is supported with
`prefix`, `delimiter`, `marker`, `max-keys` and `NextMarker`.
The V1 marker and V2 continuation token are separate protocols; mixing
them is rejected. Both V1 and V2 support `encoding-type=url`, percent
encoding UTF-8 key names and XML control characters inside XML responses.
The gateway uses immutable catalog entries from the same Telegram-backed
manifest system as normal GET/HEAD. Boto3 tests verify V1, V2, delimiter
pagination and encoded object names. `us-east-1` bucket discovery is exposed
through the independently scoped `s3:GetBucketLocation` permission.

**S3 bucket CORS (browser clients):** Signed `GetBucketCors`, `PutBucketCors`
and `DeleteBucketCors` manage bounded CORS XML rules, stored in SQL rather
than Telegram payloads. Scope S3 keys with `s3:GetBucketCORS` and
`s3:PutBucketCORS` (also available in WebUI/Desktop credential management).
Public `OPTIONS` preflight requests consult the bucket's configured rules,
checking the Origin, requested method and requested headers. **CORS does
not authenticate requests:** every actual S3 read/write still requires
normal SigV4 authorization and the appropriate object permissions. Rules
cannot silently expose private objects or bypass the signed request scope.

~~~python
s3.put_bucket_cors(
    Bucket="backup-bucket",
    CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": ["https://backup.example.com"],
        "AllowedMethods": ["GET", "PUT", "HEAD"],
        "AllowedHeaders": ["x-amz-*", "content-type"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 600,
    }]},
)
assert s3.get_bucket_cors(Bucket="backup-bucket")["CORSRules"][0]["MaxAgeSeconds"] == 600
~~~

Cross-origin requests should use HTTPS when accessed remotely. CORS is a
browser restriction mechanism, **not** a bucket policy, firewall or a
substitute for signed credentials. Hostile XML, unsupported methods, oversize
policies and invalid wildcard patterns are rejected. Live Postgres/MySQL
and boto3 tests cover persistence and safe cleanup.

**S3 DeleteObjects supported:** Signed `POST /bucket?delete` now accepts
up to 1,000 keys per request, standard `Quiet` mode and a verified
Content-MD5/CRC32/SHA-256 checksum along with the SigV4 payload hash.
The XML parser rejects DTDs, malformed records and unsupported `VersionId`
selectors before any key is unlinked. Existing or missing keys are reported
according to S3's batch-delete semantics. Like single-object DeleteObject,
batch deletion removes only the current SQL catalog pointer; Telegram
messages and historical manifests remain intact for verified GC.

~~~python
s3.delete_objects(
    Bucket="backup-bucket",
    Delete={"Objects": [{"Key": "old/file1.bin"}, {"Key": "old/file2.bin"}]},
)
~~~

**Conditional object reads:** S3 GET and HEAD now support ETag and
Last-Modified preconditions (`If-Match`, `If-None-Match`,
`If-Modified-Since` and `If-Unmodified-Since`). Matching cache
validators return `304 Not Modified` without downloading any Telegram
bytes; failed conditions return `412 PreconditionFailed`. Conditional
object **writes and deletes** are deliberately rejected until an atomic
compare-and-swap writer is available. They are not silently accepted.

**S3 multipart now implemented (core):** CreateMultipartUpload, UploadPart,
ListParts (paginated), ListMultipartUploads (owner-scoped, paginated),
CompleteMultipartUpload and AbortMultipartUpload. Parts are written directly
to Telegram using bounded-size chunks and are **not** published as live S3
objects until Complete. Before publication, Complete re-reads and validates
all Telegram chunks, enforces S3's 5 MiB minimum for every part except the
last, computes multipart MD5-derived ETag, and moves existing Telegram chunk
references to one committed object through a single SQL transaction. This
avoids re-uploading payload bytes to Telegram during completion. Retrying
the same completed part selection returns the committed result; a different
selection is rejected. Completed objects support full/Range GET, HEAD, and
ListObjectsV2 with the multipart ETag.

**Current multipart boundaries:** 10,000 parts per upload; uploaded parts are
limited by `TELEDRIVE_REST_MAX_UPLOAD_BYTES` (default 2 GiB per request).
Telegram Bot API download chunks remain capped to below 20 MiB (16 MiB
recommended). The current gateway requires SigV4 header-signed SHA-256
bodies. Part sizes and multipart ETags follow S3 conventions, but this is not
a complete AWS S3 implementation.

**Implemented presigned URLs:** Query-parameter AWS SigV4 presigned
GET, HEAD, DELETE and checksum-bound PUT are supported. URLs are limited
to 7 days, checked against credential revocation and permission scopes, and
rejected on signature/path/header tampering. Unlike AWS's general presigned
PUT mode, TeleDrive intentionally requires a **SHA-256 checksum included in
the URL's SignedHeaders**: bare unsigned-payload PUT links are refused.
Presigned multipart POST operations are not yet supported.

**CopyObject implemented:** Metadata-only copy now creates a new immutable
object ID that references the source's existing Telegram chunk journal,
without duplicating Telegram messages or violating the unique-message DB
constraint. Both source `s3:GetObject` and destination `s3:PutObject`
permissions are checked; `x-amz-copy-source` and ETag conditions must be
SigV4-signed. Copies remain accessible after the source key is deleted or
replaced. Copying multipart objects preserves their original multipart
ETag. CopySource supports `bucket/key` (boto3) and `/bucket/key`, and
cross-bucket copies work within the same Telegram storage.

**CopyObject limits:** MetadataDirective=REPLACE is implemented for
the supported signed HTTP headers and `x-amz-meta-*` user fields.
Unsupported source VersionId, byte-range Copy and advanced copy headers
still return explicit errors.
Physical garbage collection MUST follow shared alias references; deleting
source Telegram media while an alias exists would corrupt copied objects.

**S3 HTTP object metadata implemented:** Normal PutObject, CreateMultipartUpload
(followed by CompleteMultipartUpload), and CopyObject now preserve signed
`Content-Type`, `Content-Encoding`, `Content-Language`,
`Content-Disposition`, `Cache-Control`, `Expires`, and
`x-amz-meta-*` user fields. GetObject, HeadObject and byte-Range GET return
these fields from immutable SQL metadata without fetching metadata from
Telegram. CopyObject supports `MetadataDirective=COPY` (snapshot source
metadata) and `MetadataDirective=REPLACE` (new signed fields). An overwrite
never alters previous object manifests or historical metadata. Upload
publication and initial metadata writes share the same SQL transaction.

User metadata names must contain ASCII letters/digits/underscore/hyphen;
metadata values are limited to 1 KiB each and all metadata fields together
to 2 KiB. Unsupported S3 encryption, storage class, ACL, Object Lock,
website redirects and `aws-chunked` upload semantics are rejected before
Telegram transfer. These safeguards prevent an S3 client from receiving
false success for a durability/security feature the gateway does not provide.
Metadata is not a replacement for object contents: only payload bytes live
in Telegram.

**Bucket location discovery implemented:** Signed `GetBucketLocation` returns
the S3-compatible `us-east-1` XML location constraint. It requires its own
`s3:GetBucketLocation` credential permission and works through boto3.
Multi-region S3 routing is not supported.

**S3 Object/ Bucket Tagging implemented:** `GetObjectTagging`,
`PutObjectTagging`, `DeleteObjectTagging`, `GetBucketTagging`,
`PutBucketTagging`, `DeleteBucketTagging`. Each action has an independent
S3 key permission; normal `s3:GetObject` does **not** permit reading tags.
Tags are stored as small SQL metadata (10 tags maximum, strict key/value
limits), never as payloads on another storage backend. Tag editing does not
transfer Telegram file bytes. Object tags belong to immutable object IDs;
when a key is overwritten, the new object starts with no tags while older
version tags remain in metadata. CopyObject snapshots its source tags
atomically into the new object, so later source changes do not alter the copy.
Empty bucket deletion removes its bucket tags transactionally.

Example boto3 calls:

~~~python
s3.put_object_tagging(
    Bucket="backup-bucket", Key="test.txt",
    Tagging={"TagSet": [{"Key": "project", "Value": "TeleDrive"}]},
)
print(s3.get_object_tagging(Bucket="backup-bucket", Key="test.txt")["TagSet"])
s3.delete_object_tagging(Bucket="backup-bucket", Key="test.txt")
s3.put_bucket_tagging(
    Bucket="backup-bucket",
    Tagging={"TagSet": [{"Key": "team", "Value": "storage"}]},
)
print(s3.get_bucket_tagging(Bucket="backup-bucket")["TagSet"])
s3.delete_bucket_tagging(Bucket="backup-bucket")
~~~

**Signed inline tags are supported:** S3 `PutObject` accepts the
URL-encoded `x-amz-tagging` header only when included in AWS
`SignedHeaders` and the credential has `s3:PutObjectTagging` permission.
The tags are attached to the new immutable object in the same SQL
transaction as publishing its current head; malformed/unauthorized tags
are rejected **before** any Telegram upload. `CopyObject` also accepts
`x-amz-tagging-directive: REPLACE`, with an optional signed tagging
header, without transferring Telegram bytes. COPY of a tagged source
requires both `s3:GetObjectTagging` and `s3:PutObjectTagging` in
addition to normal copy permissions. Copy operations with replacement
tags require `s3:PutObjectTagging`.

**Remaining limits:** Version-specific `?versionId=` tagging is not
implemented and is explicitly rejected. Bucket GET without a tag set
returns S3 `NoSuchTagSet`, whereas untagged objects return an empty
tag set. XML DTD/entities, invalid MD5/SHA-256 checksums, duplicate keys
and exceeding tag limits are rejected.

**Not yet supported:** virtual-host-style buckets, AWS chunked or
checksum-unbound PUT bodies, ACL/policy APIs, version listing, lifecycle,
object lock, notifications, replication, advanced multipart checksum modes
and full SDK interoperability. Unsupported operations return explicit S3 XML
`NotImplemented` rather than simulated success. Tests cover botocore-signed
HTTP, boto3 GET/PUT/HEAD presigns, multipart/no-reupload, owner isolation,
wrong checksums, expired credentials, and live PostgreSQL/MySQL chunk moves.

Example boto3-generated short-lived URLs:

~~~python
import base64
import hashlib

body = b"Example Telegram-backed object"
sha256_b64 = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")

download_url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "backup-bucket", "Key": "test.txt"},
    ExpiresIn=300,
)
upload_url = s3.generate_presigned_url(
    "put_object",
    Params={"Bucket": "backup-bucket", "Key": "new.txt",
            "ChecksumSHA256": sha256_b64},
    ExpiresIn=300,
    HttpMethod="PUT",
)
# The client uploading to upload_url MUST send exactly:
# x-amz-checksum-sha256: <sha256_b64>
# together with the original bytes. Changing either is rejected.
~~~

Do not embed the master encryption key or S3 secret key in a shared URL.
Presigned URLs themselves grant temporary access and must not be logged,
pasted in issues or sent over insecure transport.

### Inspect Telegram references before any physical GC

WebUI **API Management → Telegram storage reference audit** now exposes a
root-admin-only, read-only inventory. It includes current object heads,
immutable CopyObject aliases, historical manifests and staged multipart
parts. It makes **no deletion decisions** and cannot remove Telegram media.

The same inventory is accessible as JSON:

~~~bash
curl -u "ADMIN_USER:ADMIN_PASSWORD" \
  "http://127.0.0.1:8080/api/v1/admin/storage/gc-audit?limit=100"
~~~

The response deliberately reports `physical_deletion_enabled: false`
and `eligible_for_deletion: 0`. Historical manifests and CopyObject
aliases remain protected until a retention-aware, version-aware GC planner
and live Telegram message ownership verifier can prove deletion safe.
Do not treat an unreferenced current object head as disposable media:
CopyObject may still depend on its original Telegram messages.

### Recover a stale S3 multipart completion (root admin only)

An interrupted `CompleteMultipartUpload` can be retried after reclaiming
an expired ownership lease. Check eligibility first:

~~~bash
curl -u "ADMIN_USER:ADMIN_PASSWORD" \
  "http://127.0.0.1:8080/api/v1/admin/s3-multipart/stale?min_age_seconds=3600"
~~~

Then request a **non-destructive preview** of one upload:

~~~bash
curl -u "ADMIN_USER:ADMIN_PASSWORD" -H "X-TeleDrive-CSRF: 1" \
  -H "Content-Type: application/json" \
  -d '{"execute":false,"min_age_seconds":3600}' \
  "http://127.0.0.1:8080/api/v1/admin/s3-multipart/UPLOAD_ID/recover"
~~~

Only if the preview reports `eligible: true`, repeat with
`"execute":true` to return the upload to `active` so the original S3
client can explicitly retry Complete or Abort. Recovery cannot run while
the lease is fresh and never deletes Telegram messages or publishes data.
The S3 credential holder is not allowed to bypass this root-admin-only
operation. Use HTTPS or a trusted VPN for any remote admin traffic.

### Kalan riskler ve yapılması gerekenler (S3 denetimi)

- **Stale multipart completions (recovery implemented):** Each in-progress
  Complete has a renewable SQL lease with an unguessable ownership token.
  Publishing the final object and moving Telegram chunk references requires
  renewing that same token in the SQL transaction. An expired worker cannot
  reset an upload claimed by another worker. Operator recovery is **dry-run
  by default**, and requires the lease to be expired for at least 1 hour by
  default. Staged Telegram messages are never deleted during recovery.
  Pre-fencing legacy `completing` rows that have no lease remain blocked
  for manual investigation (never guessed safe to recover).
- **Orphan/obsolete Telegram parts and CopyObject aliases:** The
  non-destructive GC reference audit is implemented in the WebUI and admin
  API. It accounts for CopyObject, historical manifests and multipart parts,
  but **physical deletion remains disabled**. Future mark-and-sweep GC must
  preserve all copied-object source aliases, obey immutable history/version
  retention, and verify the actual Telegram message before deletion.
- **Physical lifecycle/versioning:** DeleteObject currently unlinks the
  current pointer; historical data remains and is not yet exposed through
  complete S3 ListObjectVersions / restore APIs.
- **Protocol coverage:** Presigned GET/HEAD/DELETE and checksum-bound PUT,
  CopyObject, scoped DeleteObjects, conditional GET/HEAD, object/bucket
  tagging, signed user metadata and bucket CORS now work. Still pending: atomic conditional writes/deletes,
  presigned multipart POST, version-ID tagging, advanced S3 checksum variants,
  SSE, virtual-host-style requests, bucket policies
  and lifecycle.
- **Operational tests:** S3 client compatibility matrix (AWS CLI, rclone,
  Cyberduck), restart during upload/complete, rate limits, account revocation,
  multiple simultaneous workers, and opt-in live Telegram multipart
  execution still need full acceptance. The manual **Telegram Bot Storage E2E**
  workflow now has a `run_multipart` input; enable it explicitly to upload
  a 5 MiB+ test object to the dedicated private Telegram channel, verify
  SQL chunk reparenting, download it, and remove only that test's messages.
  It reuses `TELEDRIVE_E2E_BOT_TOKEN` and `TELEDRIVE_E2E_BOT_CHAT_ID`;
  no additional secret is needed. Automatic push runs skip this costly case.
  A green regular storage E2E does not prove multipart has been exercised
  against the live Telegram API.

No primary payload storage or fallback is added outside Telegram. SQL
holds metadata and temporary upload state only; scratch files are ephemeral.

## Telegram-only object storage: Phase 1 foundation

A new `teledrive.storage` module provides immutable object manifests and
Telegram-backed chunk I/O. Payload bytes are stored **only in Telegram**;
SQLAlchemy stores message locations, offsets, SHA-256 checksums and upload
status. Local scratch files are temporary and deleted after use.

Implemented in this phase:

- Bounded-memory chunk upload with a per-chunk DB journal.
- Fail-closed object visibility: unfinished uploads cannot be read.
- Telegram message retrieval that refreshes expired media references.
- Checksum-verified download, half-open byte ranges, and atomic output files.
- Explicit interrupted/failed upload inspection for future recovery tooling.
- Deterministic tests for corruption, missing chunks, interrupted upload, source mutation,
  chunk boundaries, empty files and SQLite restart; live PostgreSQL/MySQL tests.

**Implemented after Phase 1:** REST raw binary upload/download and HEAD,
scoped REST API credentials, API Management in WebUI/Desktop, Telegram Bot
transport, bucket/current-object catalog, and the initial separate SigV4 S3
gateway with S3 credential management in both user interfaces.

**Still not implemented:** automatic recovery of still-pending uploads,
advanced multipart/S3 operations, real version listing,
physical garbage collection of superseded live objects, full resume/job
orchestration, and migration of legacy `files` records. The legacy folder
uploader remains a separate workflow until it is migrated into the common
object-service pipeline.

### Phase 2: conservative failure recovery and orphan cleanup

`teledrive.storage.RecoveryManager` now reconciles the recorded chunk journal
with **Telegram messages in configured targets**. A process can crash after a
successful Telegram upload but before recording its message ID in SQL; the
upload caption contains a stable `teledrive:<object-id>:<part>:<sha-prefix>`
marker so unindexed messages can be discovered.

- **Dry-run is always the default**, and no background process deletes messages.
- Only objects in `failed` status and older than a safety window (24 hours
  by default) can be cleaned. `pending` and `ready` objects are excluded.
- Cleanup requires a complete Telegram history scan. A scan limit, missing
  account/target, conflicting marker or unverified ownership blocks deletion.
- Every message is checked as an outgoing upload with the exact object marker
  before deletion, then Telegram deletion is verified. Per-object SQL leases
  serialize cleanup and interrupted deletion is safely retryable.
- Cleaned objects retain their audit journal but cannot be read as live objects.

**Important limits:** The recovery service is an internal component, not yet
exposed through WebUI, Desktop or REST. Hard-killed workers can leave stale
`pending` objects; writer heartbeats/fencing and explicit abandonment must be
implemented before those become cleanup candidates. Changed Telegram targets,
inaccessible history, or large histories beyond the scan limit prevent reliable
orphan discovery, so cleanup refuses to claim success. Metadata DB rebuild
from Telegram is also a later phase.

CI exercises unindexed crash orphans, corrupt/missing marker ownership,
incomplete scans, partial deletion retry, safe exclusion of READY objects
and database cleanup transactions on live PostgreSQL/MySQL.

### Telegram user + bot storage E2E (private channel)

TeleDrive keeps **two distinct Telegram upload paths**:
- **User account (Telethon / MTProto):** full user session; can resolve a
  private channel from its title and recover historical messages.
- **Bot account (official Bot HTTP API):** token-based storage and CI without
  a user session, but private-channel access requires a numeric chat ID and
  channel administrator permissions. Bot API cannot retrieve arbitrary past
  channel messages using message IDs alone, so TeleDrive persistently stores
  the uploaded document's file ID in the SQL \`storage_bot_file_ids\` table.

Both routes use the **same TelegramObjectStorage manifest, SHA-256 verification,
range reads, and Telegram-only payload storage**. Bot API index-based uploads
currently do **not** implement the MTProto recovery/orphan-discovery interface;
that difference is intentionally explicit and will need additional design
before full production feature parity.

#### One-time setup for both modes

1. Create a **dedicated Telegram test user account** and get its API ID and API
   hash from [my.telegram.org](https://my.telegram.org) → API development tools.
2. Use Telegram's [@BotFather](https://t.me/BotFather) to create a dedicated
   test bot and obtain its **bot token**. API ID/hash are not sufficient to
   authenticate a bot with the Bot HTTP API.
3. Install [GitHub CLI](https://cli.github.com/) locally and authenticate.
   With Python 3.11+ and Telethon installed, run from the TeleDrive repository:

~~~bash
gh auth login
python -m pip install -e .
python scripts/setup_telegram_e2e.py --channel "TeleDrive E2E Storage" --create-if-missing --configure-bot
~~~

The script asks once for API ID, API hash, Telegram test phone, and the
Telegram login code. A **2FA password is requested only if 2FA is enabled**.
It creates or finds the *private broadcast channel* under that user, and
refuses ambiguous duplicate names. The script prompts **locally** for your
BotFather token and checks it via the Bot API.

The script stores the following values directly through GitHub CLI, without
displaying or writing the Telegram StringSession to a session file:

| GitHub Actions setting | Type | Value |
| --- | --- | --- |
| \`TELEDRIVE_E2E_API_ID\` | Secret | Test user's Telegram API ID |
| \`TELEDRIVE_E2E_API_HASH\` | Secret | Test user's API hash |
| \`TELEDRIVE_E2E_SESSION\` | Secret | Authorized user StringSession |
| \`TELEDRIVE_E2E_CHANNEL_NAME\` | Secret | Private test channel title |
| \`TELEDRIVE_E2E_BOT_TOKEN\` | Secret | Test bot token from BotFather |
| \`TELEDRIVE_E2E_BOT_CHAT_ID\` | Variable | Numeric private channel ID resolved by test user |

**Manual Telegram permission step:** Add the dedicated bot as an
**administrator of the private test channel** and grant it *Post Messages*
and *Delete Messages*. A bot cannot list all private channels by name, and
GitHub Actions cannot infer a Telegram user's numeric channel ID from API
ID and API hash alone. The local bootstrap handles the channel-ID lookup.

If you only need the user-account mode, omit \`--configure-bot\`. The bot E2E
will be skipped until its bot token is configured. Do not give the bot access
to personal or production channels.

#### Automated CI and tests

- \`.github/workflows/telegram-storage-e2e.yml\`: real **user account** E2E.
- \`.github/workflows/telegram-bot-e2e.yml\`: real **bot account** E2E.
- Both run on pushes to \`main\` and \`feature/docker-webui-multiaccount\`,
  as well as manual dispatch. They **skip with a notice when credentials
  are not configured**; a green skipped run is not proof of real access.
- \`tests/test_telegram_target_resolution.py\` and \`tests/test_bot_storage.py\`
  run offline on Security CI, without any Telegram secrets.
- Each live test uploads a fresh 4,097-byte object in three Telegram
  messages, downloads and verifies its SHA-256, tests a cross-chunk
  [start,end) range, and deletes only the message IDs recorded for the run.

The **official cloud Bot API** currently permits uploads up to 50 MB but
downloads only up to **20 MB per file**, so use bot chunk sizes at or below
16 MiB for production-like workloads; larger Telegram user account chunks
do not have that same Bot API restriction. The Bot API index records metadata,
not payload bytes. Recovery after a remote upload succeeds but before
SQL journaling completes needs further work in bot mode.

Keep both tokens and sessions out of chat messages, screenshots, CI logs and
source control. Revoke a compromised user session in Telegram Settings →
Devices, or regenerate a compromised bot token through @BotFather. Real
Telegram E2E runs **cannot succeed** until their required credentials and
permissions exist. Workflow dispatch requires the YAML to be on the repo's
default branch; push-triggered runs can run directly on the feature branch.

## Credits and license

Originally created by [Amjad P A](https://github.com/amjadlle/TeleDrive). This is the [babico/TeleDrive fork](https://github.com/babico/TeleDrive) with Docker and multi-account enhancements. Licensed under [MIT](LICENSE); see [CONTRIBUTING.md](CONTRIBUTING.md) for contributions.
