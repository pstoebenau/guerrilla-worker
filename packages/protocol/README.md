# Worker protocol 1

Outbound JSON over HTTPS under `/api/worker/`. Only enrollment is unauthenticated;
all other calls use the revocable worker Bearer credential. Loopback HTTP is for
local development only. Transfers use short-lived presigned URLs, never storage
credentials. URLs and credentials must not appear in logs.

`enroll`, `poll`, `heartbeat`, `events`, `artifacts`, `complete`, `checkpoint`,
`refresh`, and `finish` are POST endpoints. Each attempt operation carries its
opaque fence. Event sequences start at one; event IDs make retransmission
idempotent. Lease loss stops the subprocess tree. A heartbeat cannot revive an
expired lease. Checkpoint acknowledgement follows durable canonical commit.

Artifact allocation uses an idempotent client ID. Upload URLs target staging;
the service verifies bytes and copies to an immutable canonical object. Workers
cannot select bucket keys. Multipart completion and abort belong to the service.
Refresh requires a current lease and existing authorized manifest or intent.

The package is versioned separately from runtime images. Development platform
consumption uses a checked-in package tarball; production must pin a reviewed
published version and worker image digest. Protocol compatibility does not prove
checkpoint compatibility across engine builds or operating systems.
