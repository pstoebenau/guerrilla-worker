# Security reporting

Do not include credentials, enrollment tokens, signed transfer URLs, customer
media or private platform information in public issues. Once the public
repository exists, use GitHub's private vulnerability reporting facility if
enabled; otherwise contact the repository owner privately before sharing details.
No public reporting endpoint or response-time commitment is established yet.

The worker is an untrusted execution client. Account authorization, billing,
leases, immutable object verification and scheduling belong to the control plane.
No worker receives database, general bucket, or cloud-provider credentials.
Control traffic requires HTTPS except loopback development. Transfer URLs are
bearer secrets and expire; revoking a worker cannot revoke an already issued URL.

Source input URLs are restricted to publicly routed addresses with DNS results
pinned for each connection and redirect. This is not general container network
isolation. Operators and cloud hosts executing a scan can inspect its input.
Use private scratch and credential mounts, preserve the shared GPU lock, and do
not mount the Docker socket or platform source/secrets into workers.

Protocol 1 and worker 0.1.0 are local candidates, not a security support promise.
Release support starts only after license, publication, and acceptance gates pass.
