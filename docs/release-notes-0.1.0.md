# 0.1.0 candidate — protocol 1.0.0

Local preparation only. No source release, image push, production deployment or
remote GPU acceptance has occurred. The owner accepted GPL-3.0-only for worker
code. Publication remains blocked by unresolved third-party redistribution terms
and the separate protocol license decision.

Includes independent Spirula/LichtFeld pipeline source, standalone desktop CLI,
enrolled outbound worker, owner credentials, fenced leases, verified presigned
transfers, immutable checkpoint acknowledgments, native process supervision and
shared GPU exclusion. Desktop success verifies matching SOG/SPZ counts within the
requested cap. No SuperSplat publication or private platform package is included.

Software validation: 12 agent tests, 4 legal-packaging tests; 70 pipeline Python tests (4 platform skips on Windows,
2 on Linux); standalone Docker worker/test image builds; local CLI help; container
pycolmap/PyTorch imports; injected-GPU LichtFeld version. These checks do not
establish full GPU workflow acceptance. Native scan acceptance is tracked by the
private integration validation report; copy only reviewed evidence into a public
release after it finishes.

## Hardware and compatibility

| Environment | Candidate status | Acceptance still required |
| --- | --- | --- |
| Native Windows x64, local NVIDIA RTX 4090 | Preflight and process-containment tests pass; complete engines preserved | New protocol end-to-end scans and resume evidence |
| Linux x64 NVIDIA Docker | Independently builds; CUDA engine imports/version smoke pass | Both complete engine workflows on real Linux |
| Windows Docker/WSL2 | Experimental; NVIDIA Vulkan was unavailable for Spirula | Do not advertise Spirula support from CUDA visibility alone |
| Runpod Secure/Community 3090, 4090, 5090 | No remote acceptance | Explicitly authorized paid hardware/runtime/transfer tests |
| Multiple visible GPUs | Rejected in v1 | Isolate one device per worker and share host GPU locking |

Checkpoint compatibility requires matching input/settings and runtime identities.
Windows/Linux checkpoint interchange is not established. Native package upgrades
need revalidation; native runtime identity does not freeze all Python dependencies.

Known distribution blockers: missing license on the pinned COLMAP plugin,
DINOv3 custom restrictions/compatibility, model-weight rights/provenance, and
unfinished transitive binary-notice review. See dependency-review.md.
