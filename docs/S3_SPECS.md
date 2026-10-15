# S3 API Specifications and Compatibility Matrix

> **Scope:** TeleDrive's S3-compatible listener over the shared SQL catalog and selected Telegram user-MTProto/Bot object engine, **not** AWS S3. **Last reviewed:** 2026-10-15 against the development default branch. **Target:** P2 Advanced S3 planning; this document does not assert production certification or update the released v1.0.7 binary.
>
> **Canonical operation inventory:** [AWS S3 REST API, 2006-03-01, operation list](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Operations_Amazon_Simple_Storage_Service.html). This matrix enumerates **all 116 operations** on that AWS S3 service-reference page as reviewed. AWS S3 Control, S3 Tables, S3 on Outposts, S3 Files and future additions are **separate services**, not silently included in this list. This is a protocol/API coverage inventory, not a claim to implement every possible request parameter, header, mode or AWS service.

## 1. Read the status correctly

| Status | Meaning |
| --- | --- |
| ✅ Supported | Implemented for the explicitly documented TeleDrive contract and backed by focused regression coverage; **not** a claim of complete AWS parity. |
| 🟡 Partial | Route/behavior exists, but significant AWS options, response fields or state transitions are deliberately restricted. Treat unsupported variants as unsupported. |
| ❌ Unsupported | No supported TeleDrive contract; do not rely on that AWS method. It may be rejected as NotImplemented (501) **or** require additional fail-closed routing tests; do not assume every selector currently returns the same error. |
| Not verified | Client, deployment configuration or optional parameter is untested; never upgrade an operation's status merely because another client happens to succeed. |

**Inventory totals:** 11 supported, 27 partial, 78 unsupported; **116 AWS S3 operations**. Count operations, not request permutations. Partial operations are **not** fully supported. These statuses describe development code and tests, not a fresh CI run or independently validated production setup.

**Machine-readable inventory:** [116-operation JSON manifest](../teledrive/s3/operation_manifest.json) is checked against every row of this matrix in [fail-closed route tests](../tests/test_s3_route_guard.py). Supported AWS subresources are explicitly allowlisted before the signer, SQL and Telegram transports can mutate data; unknown selectors cannot silently become normal bucket/object operations. This addresses a P2-0 subtask, **not** the entire protocol parity gate.

**Implementation evidence:** [gateway router](../web/s3.py), [S3 protocol modules](../teledrive/s3/), [S3 tests](../tests/), [CI guide](CI_WORKFLOWS.md), [implementation/production status](IMPLEMENTATION_STATUS.md). On a new code SHA, rerun its own CI rather than recycling a historical green status. No new test execution was performed solely to prepare this inventory.

## 2. Platform contract and parameter-level specifications

| Specification area | TeleDrive behavior / boundary | Evidence or next step |
| --- | --- | --- |
| Deployment and object identity | Web/REST port 8080 and S3 port 9000 in **one default container**; S3, REST and explicitly queued local-file jobs share the catalog, SQL heads, versions and Telegram chunk payloads. | [Unified architecture](IMPLEMENTATION_STATUS.md#2-architecture-and-unified-storage-contract), [integration tests](../tests/test_unified_service.py) |
| Authentication | Scoped persisted S3 keys and **AWS Signature V4** header signing; access-key secrets encrypted at rest in SQL via an external master key. REST Bearer credentials are **not** S3 credentials. | [SigV4](../teledrive/s3/sigv4.py), [credentials](../teledrive/s3/credentials.py), [auth tests](../tests/test_s3_sigv4_defense.py) |
| Signature variants | Query presigns with expiry **1–604,800 seconds**, limited methods and stricter checksum-bound PUT/multipart POST rules. SigV2, STS/session tokens and aws-chunked streaming are not implemented. | [SigV4 code](../teledrive/s3/sigv4.py), [presigned tests](../tests/test_s3_presigned.py), [multipart presigns](../tests/test_s3_presigned_complete.py) |
| Browser POST | Restricted SigV4 policy forms; checksum-pinned, maximum **20 MiB form** and policy expiry within **15 minutes**; not arbitrary AWS POST policy clauses. | [POST policy](../teledrive/s3/post_policy.py), [POST tests](../tests/test_s3_post_policy.py) |
| Addressing/regions | Path-style endpoint is the documented client default; limited configured virtual-host addressing exists. Bucket location reports us-east-1-compatible empty constraint. ListBuckets supports paginated bucket inventory scoped to this private us-east-1 region, with immutable SQL CreationDate. No AWS multi-region, Access Point ARN or directory-bucket routing. | [gateway](../web/s3.py), [virtual-host tests](../tests/test_s3_vhost.py), [location tests](../tests/test_s3_bucket_location.py) |
| Object payloads | PUT/GET/HEAD with immutable manifest, Telegram USER MTProto or Bot chunks (a user may read historic bot media by original message ID in the same channel), SHA-256 integrity and byte-range GET. Default REST/S3 single-transfer upload ceiling is **2 GiB**, configurable via TELEDRIVE_REST_MAX_UPLOAD_BYTES; actual deploy/Telegram limits apply. | [gateway](../web/s3.py), [storage tests](../tests/test_storage_engine.py) |
| ETag | TeleDrive single PUT returns a SHA-256-derived ETag; multipart has its own ETag representation. **Never assume AWS single-part MD5 ETag equality.** | [gateway](../web/s3.py), [multipart](../teledrive/s3/multipart.py), [contract tests](../tests/test_s3_contract.py) |
| Checksums on upload | Integrity validation for signed SHA-256, SHA-1, Content-MD5, CRC32, CRC32C and CRC64NVME on implemented PUT/UploadPart paths. When `x-amz-checksum-mode: ENABLED` is supplied for an object GET/HEAD, return the actual immutable full-object SHA-256 checksum; GetObjectAttributes can expose the same digest as `ChecksumSHA256` with `FULL_OBJECT` semantics. **Not** a guarantee of all SDK trailer/checksum algorithms or AWS multipart composite checksums. | [checksum helper](../teledrive/s3/checksums.py), [additional checksum tests](../tests/test_s3_additional_checksums.py) |
| Read preconditions | Selected If-Match, If-None-Match, If-Modified-Since and If-Unmodified-Since for GET/HEAD; byte ranges supported. | [conditions](../teledrive/s3/conditions.py), [tests](../tests/test_s3_conditions.py) |
| Write preconditions | Signed If-Match/If-None-Match on bounded simple PUT/DELETE and multipart completion; date-based write preconditions and conditional explicit-version deletes are not implemented. | [write conditions](../teledrive/s3/write_conditions.py), [tests](../tests/test_s3_conditional_writes.py) |
| Metadata | Selected signed HTTP metadata and x-amz-meta-* persisted per version, limited to **2 KiB serialized**; metadata and copy directives restricted. | [object metadata](../teledrive/s3/object_metadata.py), [metadata tests](../tests/test_s3_metadata.py) |
| Versioning and delete markers | Each publish retains a version; GET/HEAD and tags may select a published VersionId, list versions and delete markers. Only **Enabled** accepted for Get/PutBucketVersioning; no Suspended/null-version/MFADelete semantics. DeleteObjects cannot select individual VersionIds. | [versioning](../teledrive/s3/versioning.py), [version/read/delete tests](../tests/test_s3_bucket_versioning.py), [marker tests](../tests/test_s3_delete_markers.py) |
| Bucket/object tags | Bucket tags support the AWS general-purpose bucket 50-tag limit with case-sensitive UTF-16-length-validated keys/values, atomic replacement, scoped permissions, verified request integrity and get/delete; object tags remain limited to 10 per immutable version. | [tagging](../teledrive/s3/tagging.py), [tag tests](../tests/test_s3_tagging.py) |
| Bucket CORS | Restricted origin/method/header CORS rules, max **100** rules, **64 KiB** config. CORS is not authentication. | [CORS](../teledrive/s3/cors.py), [tests](../tests/test_s3_cors.py) |
| Public access | **Private only.** Get/PutPublicAccessBlock expose/accept all four values as true. GetBucketPolicyStatus is read-only and always IsPublic=false because public grants/policies cannot be configured. Non-private ACLs and public bucket policies are **not** supported. | [public access module](../teledrive/s3/public_access.py), [tests](../tests/test_s3_public_access_block.py) |
| Multipart | Durable staged parts, up to **10,000** part numbers, minimum **5 MiB** for non-final completed parts, size/part limits, guarded completion and abort. Completion atomically records an immutable object-version part snapshot (number/size/SHA-256/ETag); historical and copied multipart attributes have bounded 1–1,000 part pages and a read-only pre-upgrade SQL fallback. Failed/aborted/unused Telegram chunks remain protected until approved GC. | [multipart](../teledrive/s3/multipart.py), [tests](../tests/test_s3_multipart.py), [recovery tests](../tests/test_s3_recovery.py) |
| Copy | Metadata-level local bucket/key and selected source-VersionId copy; signed source, permissions, metadata/tag directives and ETag source preconditions. No UploadPartCopy, presigned CopyObject or ranged source copy. | [gateway copy route](../web/s3.py), [tests](../tests/test_s3_copy.py), [version copy](../tests/test_s3_version_copy.py) |
| Unsupported write semantics | SSE-S3/SSE-KMS/SSE-C, retention/Object Lock, public ACLs, non-STANDARD tiers, request-payer, aws-chunked/trailer semantics and select copy conditions **must not be silently acknowledged**. | [fail-closed write checks](../web/s3.py), [tests](../tests/test_s3_fail_closed.py) |
| Clients and operational proof | Boto3, AWS CLI and rclone scenarios exist, with **synthetic Telegram transport** for most storage integration tests; dedicated live Telegram E2E tests validate other actual payload paths. **Cyberduck, other SDKs and every SDK checksum mode remain unverified.** | [client tests](../tests/test_s3_cli_compat.py), [CI guide](CI_WORKFLOWS.md) |
| Physical delete and lifecycle | Object deletion removes catalog visibility or version references; it **does not authorize deleting Telegram messages**. Only SQL-only AbortIncompleteMultipartUpload lifecycle rules are implemented; an hourly task marks old unfinished upload sessions aborted while retaining Telegram chunks. Object expiration/transitions and physical GC are not enabled. | [Lifecycle](../teledrive/s3/lifecycle.py), [lifecycle tests](../tests/test_s3_lifecycle.py), [GC safeguards](IMPLEMENTATION_STATUS.md#41-physical-telegram-garbage-collection-disabled) |

## 3. Implemented and partially implemented AWS S3 actions

The following operations have a routed implementation. Every qualification in the right column is part of the contract.

| AWS S3 API operation | Status | Implemented scope / explicit gap |
| --- | --- | --- |
| [CreateBucket](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CreateBucket.html) | ✅ Supported | Creates a bucket in the shared catalog. |
| [HeadBucket](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadBucket.html) | ✅ Supported | Authenticated existence check. |
| [DeleteBucket](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucket.html) | ✅ Supported | Requires an empty/deletable catalog bucket. |
| [ListBuckets](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListBuckets.html) | ✅ Supported | Private single-region us-east-1 contract: signed GET, creation date, owner ID, 1–10,000 max-buckets, prefix/region filters, HMAC-scoped continuation token and keyset pagination. AWS account quota APIs, multi-region and directory buckets do not exist in TeleDrive. |
| [GetBucketLocation](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketLocation.html) | 🟡 Partial | Only the us-east-1-compatible empty LocationConstraint. |
| [ListObjects](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjects.html) | 🟡 Partial | V1 prefix, delimiter, marker and bounded max-keys. |
| [ListObjectsV2](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html) | 🟡 Partial | V2 continuation, start-after, prefix, delimiter, encoding-type=url; bounded max-keys. |
| [ListObjectVersions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html) | 🟡 Partial | Published versions/delete markers; restricted selectors and pagination. |
| [PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html) | 🟡 Partial | Size-bounded, checksum-verified upload with metadata/tags and version creation; no SSE, Object Lock or aws-chunked. |
| [GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html) | 🟡 Partial | Full, byte-range and selected VersionId reads, ETag/date conditions; no archive or SSE behavior. |
| [HeadObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html) | 🟡 Partial | Basic object metadata and selected VersionId/read preconditions; not all AWS headers. |
| [DeleteObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html) | 🟡 Partial | Creates logical delete marker or deletes an explicit published VersionId; does not delete Telegram messages. |
| [DeleteObjects](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObjects.html) | 🟡 Partial | Up to 1,000 key-only entries; no per-entry VersionId, MFA or governance bypass. |
| [CopyObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html) | 🟡 Partial | Catalog-local source/version copy, directives and ETag conditions; no presigned/range/date-based copy. |
| [GetObjectAttributes](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectAttributes.html) | ✅ Supported | Documented private single-region general-purpose contract: signed ETag, ObjectSize, SHA-256 FULL_OBJECT Checksum, fixed STANDARD StorageClass, published historical VersionId, LastModified header, and immutable completed-multipart ObjectParts (selected part number/size/SHA-256). Supports signed MaxParts 1–1,000, PartNumberMarker 0–10,000, IsTruncated/NextPartNumberMarker and copied version snapshots. Non-MPU objects never fabricate parts; old SQL completion journals are read only and strictly checked. Fails closed on invalid/unsigned inputs and inconsistent history. AWS SSE-C, RequestPayer, directory buckets and provider-managed tiers are outside the TeleDrive contract. |
| [CreateMultipartUpload](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CreateMultipartUpload.html) | 🟡 Partial | Signed initiation and selected metadata; no SSE/retention/storage-tier variants. |
| [UploadPart](https://docs.aws.amazon.com/AmazonS3/latest/API/API_UploadPart.html) | 🟡 Partial | 1–10,000 part numbers, configured part-size bound and selected integrity checks. |
| [ListParts](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListParts.html) | ✅ Supported | TeleDrive private general-purpose contract: scoped active uploads, SQL-bounded 1–1,000 part pages, part-number marker 0–10,000, sparse/replaced parts, accurate ETag, Size, LastModified, STANDARD StorageClass, IsTruncated/NextPartNumberMarker. Invalid/over-limit cursors and wrong-owner/inactive IDs fail closed without Telegram reads. AWS Owner/Initiator account identities, multipart composite checksums, other tiers/directory buckets remain outside the contract. |
| [ListMultipartUploads](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListMultipartUploads.html) | ✅ Supported | TeleDrive private general-purpose contract: scoped active sessions, bounded SQL keyset pages 1–1,000, prefix, delimiter/CommonPrefixes, UTF-8 encoding-type=url, same-key UploadId cursors, Initiated/StorageClass and NextKeyMarker/NextUploadIdMarker. Completed, aborted and foreign-owner sessions excluded. AWS billing/requester pays, account ownership identities and directory buckets remain outside scope. |
| [CompleteMultipartUpload](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html) | 🟡 Partial | Fenced, zero-reupload staged-part publication; selected SHA-bound requests only. |
| [AbortMultipartUpload](https://docs.aws.amazon.com/AmazonS3/latest/API/API_AbortMultipartUpload.html) | 🟡 Partial | Logical abort; remote orphan media remains protected pending approved GC. |
| [GetBucketTagging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketTagging.html) | ✅ Supported | Reads bucket tags; missing tag set reports NoSuchTagSet. |
| [PutBucketTagging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketTagging.html) | ✅ Supported | Full general-purpose private-bucket tagging contract: 0–50 unique tags, 128/256 UTF-16-unit limits, signed checksum-verified XML, atomic replacement and correct HTTP 200; AWS billing/ABAC services not emulated. |
| [DeleteBucketTagging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketTagging.html) | ✅ Supported | Removes stored bucket tag set. |
| [GetObjectTagging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectTagging.html) | 🟡 Partial | Current/published VersionId tags with scoped access. |
| [PutObjectTagging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectTagging.html) | 🟡 Partial | Current/published VersionId tags with ownership checks. |
| [DeleteObjectTagging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObjectTagging.html) | 🟡 Partial | Current/published VersionId tags with ownership checks. |
| [GetBucketCors](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketCors.html) | 🟡 Partial | Reads restricted CORS rules (maximum 100 rules). |
| [PutBucketCors](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketCors.html) | 🟡 Partial | Limited 64 KiB XML grammar, origins and allowed methods. |
| [DeleteBucketCors](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketCors.html) | ✅ Supported | Removes CORS configuration. |
| [GetBucketVersioning](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketVersioning.html) | 🟡 Partial | Always Enabled; not AWS unversioned/suspended states. |
| [PutBucketVersioning](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketVersioning.html) | 🟡 Partial | Idempotent Enabled only; rejects Suspended and MFADelete. |
| [GetPublicAccessBlock](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetPublicAccessBlock.html) | 🟡 Partial | Fixed all-four-fields true, no public mode. |
| [GetBucketPolicyStatus](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketPolicyStatus.html) | 🟡 Partial | Read-only IsPublic=false for the enforced private-only gateway; no bucket policy engine or public ACLs. |
| [PutPublicAccessBlock](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutPublicAccessBlock.html) | 🟡 Partial | Accepts only all-four-fields true; cannot weaken privacy. |
| [GetBucketLifecycleConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketLifecycleConfiguration.html) | 🟡 Partial | Read a private bucket's bounded, prefix-scoped AbortIncompleteMultipartUpload-only rules; no AWS expiration/transition support. |
| [PutBucketLifecycleConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketLifecycleConfiguration.html) | 🟡 Partial | Signed strict XML; only Disabled/Enabled incomplete multipart abort rules with DaysAfterInitiation 1..3650 and Prefix. |
| [DeleteBucketLifecycle](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketLifecycle.html) | 🟡 Partial | Removes SQL configuration; it never deletes an object or Telegram media. |

## 4. Unsupported AWS S3 actions grouped for the roadmap

**All operations below are unsupported even when a similarly named REST admin endpoint or internal SQL function exists.** The identifiers specify planned workstreams, **not** commitments that every AWS feature will ship in P2. Security- or cloud-specific functionality may remain intentionally unsupported. Grouping is exhaustive for the reference inventory above.

### P2-C — Access control, ACLs, policies and ownership

Keep private-by-default security. Define either a tested private-only compatibility response or a real policy/ownership engine before advertising parity.

| AWS S3 API operation | Status | Target |
| --- | --- | --- |
| [DeletePublicAccessBlock](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeletePublicAccessBlock.html) | ❌ Unsupported | P2-C |
| [GetBucketAcl](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketAcl.html) | ❌ Unsupported | P2-C |
| [PutBucketAcl](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketAcl.html) | ❌ Unsupported | P2-C |
| [GetObjectAcl](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectAcl.html) | ❌ Unsupported | P2-C |
| [PutObjectAcl](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectAcl.html) | ❌ Unsupported | P2-C |
| [GetBucketOwnershipControls](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketOwnershipControls.html) | ❌ Unsupported | P2-C |
| [PutBucketOwnershipControls](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketOwnershipControls.html) | ❌ Unsupported | P2-C |
| [DeleteBucketOwnershipControls](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketOwnershipControls.html) | ❌ Unsupported | P2-C |
| [GetBucketPolicy](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketPolicy.html) | ❌ Unsupported | P2-C |
| [PutBucketPolicy](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketPolicy.html) | ❌ Unsupported | P2-C |
| [DeleteBucketPolicy](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketPolicy.html) | ❌ Unsupported | P2-C |

### P2-B — Encryption and immutable retention

Implement actual encryption/key lifecycle and enforce per-version retention and legal-hold semantics across REST/S3/local jobs, copy, version delete and eventual physical GC.

| AWS S3 API operation | Status | Target |
| --- | --- | --- |
| [GetBucketEncryption](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketEncryption.html) | ❌ Unsupported | P2-B |
| [PutBucketEncryption](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketEncryption.html) | ❌ Unsupported | P2-B |
| [DeleteBucketEncryption](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketEncryption.html) | ❌ Unsupported | P2-B |
| [GetObjectLockConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectLockConfiguration.html) | ❌ Unsupported | P2-B |
| [PutObjectLockConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectLockConfiguration.html) | ❌ Unsupported | P2-B |
| [GetObjectRetention](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectRetention.html) | ❌ Unsupported | P2-B |
| [PutObjectRetention](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectRetention.html) | ❌ Unsupported | P2-B |
| [GetObjectLegalHold](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectLegalHold.html) | ❌ Unsupported | P2-B |
| [PutObjectLegalHold](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectLegalHold.html) | ❌ Unsupported | P2-B |

### P2-D — Lifecycle, expiration, transitions, replication and restore

Model rules and durable workers without promising AWS transitions that Telegram cannot provide. Physical expiration/deletion requires independent backup, writer fences and two-person authorization.

| AWS S3 API operation | Status | Target |
| --- | --- | --- |
| [GetBucketLifecycle](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketLifecycle.html) | ❌ Unsupported | P2-D |
| [PutBucketLifecycle](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketLifecycle.html) | ❌ Unsupported | P2-D |
| [GetBucketReplication](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketReplication.html) | ❌ Unsupported | P2-D |
| [PutBucketReplication](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketReplication.html) | ❌ Unsupported | P2-D |
| [DeleteBucketReplication](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketReplication.html) | ❌ Unsupported | P2-D |
| [RestoreObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_RestoreObject.html) | ❌ Unsupported | P2-D |

### P2-E — Advanced data-plane protocol compatibility

Support copy-part source ranges, signing and ownership, checksum composition and crash/retry semantics; also address parameter/header gaps for already partial operations.

| AWS S3 API operation | Status | Target |
| --- | --- | --- |
| [UploadPartCopy](https://docs.aws.amazon.com/AmazonS3/latest/API/API_UploadPartCopy.html) | ❌ Unsupported | P2-E |

### P2-F — Website, notifications and access logging

Specify whether private-only hosting permits website mode; use transactional outbox for events, durable retries, scoped destinations and secret-safe log delivery.

| AWS S3 API operation | Status | Target |
| --- | --- | --- |
| [GetBucketWebsite](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketWebsite.html) | ❌ Unsupported | P2-F |
| [PutBucketWebsite](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketWebsite.html) | ❌ Unsupported | P2-F |
| [DeleteBucketWebsite](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketWebsite.html) | ❌ Unsupported | P2-F |
| [GetBucketNotification](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketNotification.html) | ❌ Unsupported | P2-F |
| [GetBucketNotificationConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketNotificationConfiguration.html) | ❌ Unsupported | P2-F |
| [PutBucketNotification](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketNotification.html) | ❌ Unsupported | P2-F |
| [PutBucketNotificationConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketNotificationConfiguration.html) | ❌ Unsupported | P2-F |
| [GetBucketLogging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketLogging.html) | ❌ Unsupported | P2-F |
| [PutBucketLogging](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketLogging.html) | ❌ Unsupported | P2-F |

### P3-G — Analytics, inventory, metrics and tier policies

Requires schedulers/report delivery and storage-tier accounting; out of P2 core. Telegram storage has no AWS Glacier/Intelligent-Tiering equivalence.

| AWS S3 API operation | Status | Target |
| --- | --- | --- |
| [GetBucketAnalyticsConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketAnalyticsConfiguration.html) | ❌ Unsupported | P3-G |
| [PutBucketAnalyticsConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketAnalyticsConfiguration.html) | ❌ Unsupported | P3-G |
| [DeleteBucketAnalyticsConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketAnalyticsConfiguration.html) | ❌ Unsupported | P3-G |
| [ListBucketAnalyticsConfigurations](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListBucketAnalyticsConfigurations.html) | ❌ Unsupported | P3-G |
| [GetBucketInventoryConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketInventoryConfiguration.html) | ❌ Unsupported | P3-G |
| [PutBucketInventoryConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketInventoryConfiguration.html) | ❌ Unsupported | P3-G |
| [DeleteBucketInventoryConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketInventoryConfiguration.html) | ❌ Unsupported | P3-G |
| [ListBucketInventoryConfigurations](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListBucketInventoryConfigurations.html) | ❌ Unsupported | P3-G |
| [GetBucketMetricsConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketMetricsConfiguration.html) | ❌ Unsupported | P3-G |
| [PutBucketMetricsConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketMetricsConfiguration.html) | ❌ Unsupported | P3-G |
| [DeleteBucketMetricsConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketMetricsConfiguration.html) | ❌ Unsupported | P3-G |
| [ListBucketMetricsConfigurations](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListBucketMetricsConfigurations.html) | ❌ Unsupported | P3-G |
| [GetBucketIntelligentTieringConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketIntelligentTieringConfiguration.html) | ❌ Unsupported | P3-G |
| [PutBucketIntelligentTieringConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketIntelligentTieringConfiguration.html) | ❌ Unsupported | P3-G |
| [DeleteBucketIntelligentTieringConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketIntelligentTieringConfiguration.html) | ❌ Unsupported | P3-G |
| [ListBucketIntelligentTieringConfigurations](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListBucketIntelligentTieringConfigurations.html) | ❌ Unsupported | P3-G |

### P3-H — AWS-managed products, directory buckets, annotations and provider services

Mostly depends on AWS account services (Express sessions, directory buckets, S3 Metadata, Object Lambda, acceleration, billing), not generic S3-compatible object storage.

| AWS S3 API operation | Status | Target |
| --- | --- | --- |
| [CreateBucketMetadataConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CreateBucketMetadataConfiguration.html) | ❌ Unsupported | P3-H |
| [CreateBucketMetadataTableConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CreateBucketMetadataTableConfiguration.html) | ❌ Unsupported | P3-H |
| [GetBucketMetadataConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketMetadataConfiguration.html) | ❌ Unsupported | P3-H |
| [GetBucketMetadataTableConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketMetadataTableConfiguration.html) | ❌ Unsupported | P3-H |
| [DeleteBucketMetadataConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketMetadataConfiguration.html) | ❌ Unsupported | P3-H |
| [DeleteBucketMetadataTableConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteBucketMetadataTableConfiguration.html) | ❌ Unsupported | P3-H |
| [UpdateBucketMetadataAnnotationTableConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_UpdateBucketMetadataAnnotationTableConfiguration.html) | ❌ Unsupported | P3-H |
| [UpdateBucketMetadataInventoryTableConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_UpdateBucketMetadataInventoryTableConfiguration.html) | ❌ Unsupported | P3-H |
| [UpdateBucketMetadataJournalTableConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_UpdateBucketMetadataJournalTableConfiguration.html) | ❌ Unsupported | P3-H |
| [GetObjectAnnotation](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectAnnotation.html) | ❌ Unsupported | P3-H |
| [PutObjectAnnotation](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectAnnotation.html) | ❌ Unsupported | P3-H |
| [DeleteObjectAnnotation](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObjectAnnotation.html) | ❌ Unsupported | P3-H |
| [ListObjectAnnotations](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectAnnotations.html) | ❌ Unsupported | P3-H |
| [GetBucketAbac](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketAbac.html) | ❌ Unsupported | P3-H |
| [PutBucketAbac](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketAbac.html) | ❌ Unsupported | P3-H |
| [GetBucketRequestPayment](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketRequestPayment.html) | ❌ Unsupported | P3-H |
| [PutBucketRequestPayment](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketRequestPayment.html) | ❌ Unsupported | P3-H |
| [GetBucketAccelerateConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketAccelerateConfiguration.html) | ❌ Unsupported | P3-H |
| [PutBucketAccelerateConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketAccelerateConfiguration.html) | ❌ Unsupported | P3-H |
| [GetObjectTorrent](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectTorrent.html) | ❌ Unsupported | P3-H |
| [CreateSession](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CreateSession.html) | ❌ Unsupported | P3-H |
| [ListDirectoryBuckets](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListDirectoryBuckets.html) | ❌ Unsupported | P3-H |
| [RenameObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_RenameObject.html) | ❌ Unsupported | P3-H |
| [SelectObjectContent](https://docs.aws.amazon.com/AmazonS3/latest/API/API_SelectObjectContent.html) | ❌ Unsupported | P3-H |
| [UpdateObjectEncryption](https://docs.aws.amazon.com/AmazonS3/latest/API/API_UpdateObjectEncryption.html) | ❌ Unsupported | P3-H |
| [WriteGetObjectResponse](https://docs.aws.amazon.com/AmazonS3/latest/API/API_WriteGetObjectResponse.html) | ❌ Unsupported | P3-H |

## 5. P2 Advanced S3 implementation plan

| Priority | Workstream | Implementation deliverable | Acceptance gate |
| --- | --- | --- | --- |
| P2-0 | Compatibility contract and fail-closed audit | Every known AWS selector, query parameter and unsupported header routed explicitly; central operation registry and machine-readable support manifest generated from tests. | Negative tests prove unsupported requests cannot fall through as successful list/get/put/delete; all documented statuses match behavior. |
| P2-1 | P2-E: multipart, checksums and SDK parity | UploadPartCopy or explicit permanent rejection; checksum negotiation, trailers, multipart composition, VersionId/copy attributes, SDK request-mode compatibility. | boto3 + AWS CLI + rclone matrix; corruption/mismatch rejection, retries, presigns, concurrent writers, cross-client REST/S3 readback. |
| P2-2 | P2-C: policies and ownership | Decide immutable private-only model versus scoped policy engine; implement safe Get/Put/Delete semantics only if authorization is real. | Cross-tenant permissions, deny-by-default, anonymous/public denial, authorization fuzz tests. |
| P2-3 | P2-B: encryption and retention | Design managed keys, SSE at-rest semantics, key rotation and version-aware Object Lock; preserve immutable historical reads. | Restart/backup/restore and recovery proofs, byte-level validation, enforced retention on *every* delete path. |
| P2-4 | P2-D: lifecycle and replication | Durable lifecycle state machine, dry-run expiration, target/failure handling, version-aware safety fences; replication only with genuinely independent destination. | SQL crash/replay/race tests, leaked/orphan protection, no Telegram physical delete without separately approved safety gates. |
| P2-5 | P2-F: notifications/logging/website | Event outbox and subscriber retries first; hosting only if consistent with private-only policy. | Exactly-once-visible event IDs or explicit dedupe policy, backpressure, no credential leakage, restart recovery. |
| After P2 | P3-G/H: reports and AWS-specific services | Independently evaluate ROI; avoid fake AWS-specific semantics (billing, S3 Express, Object Lambda, managed metadata). | Separate design/RFC and client contracts before implementing. |

### Engineering checklist / definition of done

- [x] Commit a machine-readable 116-operation manifest synchronized by CI with the reviewed specification matrix, and add a strict S3 request-parameter allowlist. Upstream AWS additions still require explicit periodic re-audits.
- [ ] For every supported operation, test normal, missing/auth-failed, malformed, over-limit, retry, multiworker race, signature-tampered and crash/restart cases; assert no partial SQL publication.
- [ ] For unsupported operations, add adversarial selector/HTTP method tests: **no accidental HTTP 200 or mutation**; unsupported-sensitive headers must fail before Telegram I/O.
- [ ] Verify all S3 mutation paths are reflected in REST/local-job heads, version records, tombstones, tags and copy aliases; invariants run against both SQLite and PostgreSQL.
- [ ] Add boto3, AWS CLI and rclone version-pinned compatibility matrix plus optional client tests; distinguish simulated Telegram I/O from real test-channel Telegram E2E.
- [ ] Define atomic migration/rollback and API backward compatibility for every metadata table or new header field; no irreversible rewrite of historic versions.
- [ ] Respect GC blocking prerequisites: independent payload backup and restore rehearsal, owner/alias/retention graph, writer fencing and explicit independent approval **before** physical Telegram deletion.
- [ ] Update this matrix, [README](../README.md), [Project Status](IMPLEMENTATION_STATUS.md) and [CI Guide](CI_WORKFLOWS.md) **only when implementation and evidence change**; keep unsupported rows marked unsupported until validated on the new SHA.

## 6. Release and evidence boundaries

A successfully parsed or authenticated AWS request is **not** automatically a supported S3 specification. Successful synthetic tests prove only their scenario, not general AWS service parity, multi-region behavior, original-media recovery or an operator-authorized production cutover. P2 work should preserve the default **one-container** interface topology, bounded resource use, shared REST/S3/local-job catalog, and fail-closed safety guarantees documented in [Project Status](IMPLEMENTATION_STATUS.md).

**Upstream reference:** [Amazon S3 REST API operations](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Operations_Amazon_Simple_Storage_Service.html). **Repository evidence:** [S3 route](../web/s3.py), [S3 modules](../teledrive/s3/), [S3 tests](../tests/), [TODO](../TODO.md). The AWS operation inventory can grow; re-audit it before every advanced-compatibility milestone.
