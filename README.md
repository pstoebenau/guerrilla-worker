# Guerrilla Worker

Independent pipeline source and outbound HTTP worker, protocol 1.0.0. This is a
local release candidate; publication remains pending redistribution review.
It includes complete Spirula and LichtFeld workflows. It has no database driver,
bucket credential client, private package, queue access, or inbound server.

## License

The owner accepted GPL-3.0-only for the original worker code. That code is
licensed under GNU GPL version 3 only; see [LICENSE](LICENSE). Third-party
components retain their own terms. The separate `packages/protocol` contract
is licensed under MIT; see its LICENSE. RoMa/DINOv3-based processing is also
subject to the [model terms](docs/model-terms.md), including the complete
DINOv3 agreement and its use restrictions.
License acceptance does not clear the redistribution blockers in
[the dependency review](docs/dependency-review.md).

## Standalone scans

Install Python 3.12+, FFmpeg, the required engine and its documented plugins.
Create a virtual environment and install `pipeline/requirements.txt` plus
CUDA-enabled pycolmap 4.0.2 in that environment (see
[Windows instructions](docs/windows-worker.md#reconstruction-dependency); Linux
uses `pycolmap-cuda12==4.0.2`). Reconstruction uses our own direct pycolmap
pipeline; the LichtFeld COLMAP plugin is not required. Set
`LICHTFELD_BIN`, `LICHTFELD_DENSIFICATION_PLUGIN`, or
`SPIRULA_BIN` when overriding normal install locations.

```sh
python pipeline/run_scan.py video.mp4 --output output --max-cap 100000
python pipeline/run_scan.py video.mp4 --output output --max-cap 100000 --resume
```

The original desktop entry point runs LichtFeld. For either complete engine use
`python pipeline/platform_runner.py --request request.json` with backend,
maxCap, settings, inputPath and outputPath. Settings schema is in pipeline/.
No account is required. Local files, direct video URLs and Google Photos links
are supported by the desktop entry point. Completed stages and verified resume
checkpoints are preserved on failure. Never change settings during resume.

## Enrolled agent

Install Bun 1.4.2 and Node 22.22.0+. Run `bun install --frozen-lockfile`, then
`bun run build`. `PYTHON` selects the Python environment; `PIPELINE_ROOT` may
select an installed pipeline directory. `node dist/worker.mjs --preflight`
reports the actual engine/GPU availability. One agent executes one scan at a time.
Version 1 requires exactly one visible GPU; isolate Docker devices with
`--gpus device=GPU_UUID`. Multiple visible GPUs or a nonzero worker index are rejected.
All local agents sharing a GPU must share `GPU_LOCK_PATH` and
`WORKER_AGENT_LOCK_DIRECTORY`. The latter is an absolute directory holding
GPU-UUID-keyed supervisor locks acquired before polling or downloading work.
The native Windows defaults use shared Guerrilla directories. Containers must
bind the same host GPU-lock directory at `/gpu` across worktrees and workers;
separate per-container directories or volumes do not provide host-wide exclusion.
A lease loss stops the process tree.

Create an enrollment token from your account's worker settings. Set
`WORKER_CONTROL_URL` and pass the token through `WORKER_ENROLLMENT_TOKEN` or stdin:

```sh
node dist/worker.mjs enroll --token-stdin
node dist/worker.mjs
```

`WORKER_CREDENTIAL_FILE` defaults outside this checkout at
`~/.config/guerrilla-worker/credentials.json`. Protect its host directory with
owner-only permissions/ACLs. The agent waits for enrollment if it is absent.
`WORKER_SCRATCH` controls local data storage. Credentials and signed URLs must
never be copied into images or diagnostic reports. The platform verifies uploads
and commits each checkpoint before the runner acknowledges/prunes older data.
Account owners can drain, revoke, or cancel through the control plane.

## Docker and checks

```sh
bun run check
bun test
bun run build
python -m unittest discover -s pipeline/tests
docker build --target test -t guerrilla-worker:test .
docker build --target worker -t guerrilla-worker:local .
```

Set `PYTHONPATH=pipeline` for Python tests. Docker builds use only this checkout
and public dependencies. Final image is a local review candidate, not approved
for redistribution; see [dependency review](docs/dependency-review.md).
After enrollment, mount the credential file read-only, mount scratch and the
shared host GPU-lock directory at `/gpu`, pass
`WORKER_CREDENTIAL_FILE`, `WORKER_SCRATCH=/scratch` and `--gpus all`. Do not expose
ports, mount Docker sockets, or pass database/S3/provider credentials.

Linux NVIDIA is the initial Docker target. Native Windows uses the same protocol.
For a portable Windows executable using installed engines, see
[Windows worker packaging](docs/windows-worker.md).
Windows Docker/WSL previously exposed CUDA but no NVIDIA Vulkan adapter to
Spirula. No remote Linux or 3090/4090/5090 acceptance is claimed by this candidate.
There is no SuperSplat publication integration. SOG/SPZ outputs are locally verified.

## Release and security

Use fresh public history only after source review and license approval. Release
source, dependency notices, corresponding source for GPL components, protocol
version and immutable image digest together. No registry release exists yet.
Report suspected security issues privately to the repository owner; do not post
credentials, presigned URLs or customer media in public issues.
