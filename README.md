# Guerrilla Worker

Independent pipeline source and outbound HTTP worker, protocol 1.0.0. This is a
local release candidate; publication remains pending redistribution review.
It includes complete Spirula and LichtFeld workflows. It has no database driver,
bucket credential client, private package, queue access, or inbound server.

## Native worker setup

The same worker and commands run on Windows x64, Linux x64, and Apple Silicon
macOS. Spirula uses the host's hardware Vulkan GPU (MoltenVK on macOS).
LichtFeld additionally requires NVIDIA CUDA and its engine/plugins; it is optional
and is advertised only when its own preflight passes.

Install Bun, Node.js 22.22.0+, FFmpeg, and Python 3.12+ or `uv`, then run:

```sh
bun run setup
bun start --preflight
```

Setup installs the checksum-verified Spirula release, an isolated Python
environment, and the locked converter, then builds and preflights the worker.
Engines live in `~/.local/share/guerrilla-worker/runtimes/` outside the checkout.
`SPIRULA_BIN` and `PYTHON` can select an existing installation instead. Linux
also needs its GPU driver and the system libraries required by Spirula's Ubuntu
binary. Setup does not install NVIDIA/CUDA dependencies for Spirula-only workers.

Set `WORKER_CONTROL_URL` to your Guerrilla server, create a token in **My workers**,
and enroll once:

```sh
bun start enroll --token-stdin
bun start
```

Credentials stay outside the checkout. In Guerrilla, choose **Spirula** and
**My workers** for an Apple Silicon, AMD, or Intel GPU worker. For local Guerrilla
development, use `bun run worker:setup`, `bun run worker enroll --token-stdin`,
and `bun dev --worker` from the Guerrilla repository instead.

GPU discovery comes from Spirula itself, rather than an OS-specific hardware
inventory. Checkpoint freezing and process containment live in `process_runtime.py`;
the shared pipeline keeps engine commands and artifacts independent of the host.
Windows uses Job Objects, Linux uses parent-death signals, and macOS uses a process
group watchdog. These host primitives preserve GPU exclusion after supervisor exit.

## Engine architecture

`agent/` owns enrollment, leases, transfers and supervision. It runs the Python
pipeline through JSONL events; it does not build engine commands. Each engine's
registration hash includes its own runtime versions, shared pipeline/converter
identity and the worker bundle hash when available. Job startup preflights only
the requested engine.

`pipeline/platform_runner.py` owns stage manifests, selective restoration,
checkpoint freezing/archival, retention and verified SOG exports. It receives an
engine object: `Runner(request, engine=adapter)`. Normal CLI execution selects the
adapter from the explicit registry in `pipeline/engines.py`. This is dependency
injection through a constructor; there is no DI framework or plugin loader.

`pipeline/engine.py` defines the structural `Engine` contract and artifact
descriptors. `engine_lichtfeld.py` and `engine_spirula.py` implement their settings,
runtime identity, prerequisites, stage commands, checkpoint layouts and preview
conversion. Adapters use the runner's stage/restore/command methods so they share
the durability and cancellation guarantees. Native GPU imports stay in preflight.

To add an engine such as Brush, implement an adapter and register it in
`pipeline/engines.py`. Return a final PLY and describe its native checkpoints;
reuse existing reconstruction helpers where appropriate. Keep engine-specific
formats and commands inside the adapter. Add its ID to the protocol's `ENGINES`
list, then update server settings validation, UI selection and engine setup/release
packaging. An adapter alone does not enable an engine across the product. Validate
its complete workflow, Gaussian cap and checkpoint resume on the actual GPU.
`pipeline/tests/test_engines.py` exercises a third test adapter through execution,
retention, compaction and resume without changing shared orchestration.

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
Create a virtual environment and install `pipeline/requirements.txt`. LichtFeld
also needs CUDA-enabled pycolmap 4.0.2 in that environment (see
[Windows instructions](docs/windows-worker.md#reconstruction-dependency); Linux
uses `pycolmap-cuda12==4.0.2`). Reconstruction uses our own direct pycolmap
pipeline; the LichtFeld COLMAP plugin is not required. Set
`LICHTFELD_BIN`, `LICHTFELD_DENSIFICATION_PLUGIN`, or
`SPIRULA_BIN` when overriding normal install locations.

Exports use the independent PlayCanvas `splat-transform` CLI, never the training
engine. Install Node.js 22+ and run `npm ci --prefix pipeline/docker/converter`
for the locked converter, or install `@playcanvas/splat-transform@3.10.1` globally.
`SPLAT_TRANSFORM_BIN` can select its executable or `bin/cli.mjs` entry point.
Spirula scans require Spirula and this converter; LichtFeld is only required for
the LichtFeld workflow. SOG compression uses Metal on macOS, Vulkan on Linux,
and D3D12 on Windows. Set
`SPLAT_TRANSFORM_GPU=cpu` explicitly for CPU compression or a GPU adapter index;
`SPLAT_TRANSFORM_GPU_BACKEND` selects another WebGPU backend.

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
The worker requires exactly one visible hardware GPU; isolate Docker devices with
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
# Build/download the checksummed engine artifact and runtime first; see below.
docker build --target worker --build-arg WORKER_RUNTIME_IMAGE=guerrilla-runtime:local -t guerrilla-worker:local .
```

Set `PYTHONPATH=pipeline` for Python tests. Docker builds use only this checkout
and public dependencies. See [Linux build separation](docs/release-process.md#reusable-linux-runtime-and-worker-builds)
for prebuilt artifacts and single-engine targets. Final image is a local review candidate, not approved
for redistribution; see [dependency review](docs/dependency-review.md).
After enrollment, mount the credential file read-only, mount scratch and the
shared host GPU-lock directory at `/gpu`, pass
`WORKER_CREDENTIAL_FILE`, `WORKER_SCRATCH=/scratch` and `--gpus all`. Do not expose
ports, mount Docker sockets, or pass database/S3/provider credentials.

Linux NVIDIA is the Docker target. Native Windows, Linux and macOS use the same protocol.
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
