# Worker protocol 2

Licensed under MIT; see [LICENSE](LICENSE). The worker application is separately GPL-3.0-only.

Outbound JSON over HTTPS under `/api/worker/`. Only enrollment is unauthenticated;
all other calls use the revocable worker Bearer credential. Loopback HTTP is for
local development only. Transfers use short-lived presigned URLs, never storage
credentials. URLs and credentials must not appear in logs.

`enroll`, `poll`, `heartbeat`, `events`, `artifacts`, `complete`, `checkpoint`,
`refresh`, `cleanup`, and `finish` are POST endpoints. Each attempt operation carries its
opaque fence. Event sequences start at one; event IDs make retransmission
idempotent. Lease loss stops the subprocess tree. A heartbeat cannot revive an
expired lease. Checkpoint acknowledgement follows durable canonical commit.

Assignments carry scan-owned `resumeState` and verified artifact metadata across
all attempts. Workers restore only dependencies requested by the next unfinished
stage. `inheritedArtifactIds` references existing verified scan artifacts in a new
checkpoint commit without downloading or uploading those bytes again. Completed
stage manifests are immutable. Native checkpoints are recorded separately for
unfinished stages.

A heartbeat stop with `reason: "cancelled"` authorizes discarding that scan's local
scratch after processing/transfers stop. Other stops retain unsaved data. Polling
also returns outstanding `cleanup` fences, including after restart; `cleanup`
acknowledges deletion even after the execution lease expires. Old protocol-v1
workers and attempt-based resume formats are not supported by this protocol.

Artifact allocation uses an idempotent client ID. Upload URLs target staging;
the service verifies bytes and copies to an immutable canonical object. Workers
cannot select bucket keys. Multipart completion and abort belong to the service.
Refresh requires a current lease and existing authorized manifest or intent.

The package is versioned separately from runtime images. Development platform
consumption uses a checked-in package tarball; production must pin a reviewed
published version and worker image digest. Protocol compatibility does not prove
checkpoint compatibility across engine builds or operating systems.
