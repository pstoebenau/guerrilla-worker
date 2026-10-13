# Guerrilla Worker

Cross-platform management app and independent background worker for Guerrilla.
The Tauri desktop app uses Svelte/TypeScript; the enrolled agent and pipeline
orchestration use Rust. Focused Python helpers handle COLMAP, image processing,
and engine APIs. The outbound worker protocol remains version 2.

## Desktop

The desktop app runs on Windows x64, Linux x64, and macOS 14 or later (Apple Silicon and
Intel management builds). Install LichtFeld and/or Spirula through their own
upstream distribution, then select their executable paths in **Setup**. LichtFeld
requires an NVIDIA CUDA GPU and the compatible densification plugin. Apple Silicon
execution uses Spirula. Available engines are advertised only after their own
preflight passes; an unavailable engine never silently falls back to another.

Python, FFmpeg/FFprobe, Node, the export converter, and supporting Python libraries
are included in desktop packages. GPU drivers and engine installations are supplied
by the owner. Desktop build and setup commands do not build or download engines.
The Linux Docker image has a separate, fully bundled engine policy described below.

Open **Guerrilla Worker**, check dependencies, create an enrollment token on your
server's Workers page, and enroll in Setup. The main window shows actual worker
state, GPU/engine availability, current job progress, and bounded logs. It manages
server-assigned work only; it has no local scan creation flow.

Closing the window leaves the tray and worker running. **Quit interface** closes
the GUI while the independent worker continues. **Stop worker** safely interrupts
execution and retains recovery data; **Finish and pause** completes the current
job and stops claiming work. Login startup is optional and runs under your account,
not as a system service before login. Reopening attaches to the existing worker.

See [desktop operation and architecture](docs/cross-platform-worker.md).

## Build a desktop package

Developers need Bun, Rust, and the platform's Tauri build prerequisites. Users of
the resulting package do not need those tools or a system Python/Node/FFmpeg.

```sh
bun install --frozen-lockfile
bun run build
```

The build compiles both Rust executables, stages Tauri sidecars and Python helpers,
then prepares checksum-pinned runtime assets and hash-locked Python dependencies.
Runtime preparation is host-native. Windows packages use a disk-spanned Inno
installer with offline WebView2 and app-local Microsoft runtime libraries. Keep
the setup executable and every payload slice together. Windows/Linux CUDA bundles
are large because they include reconstruction libraries. Desktop packages do not
include engine binaries.

To work on the interface after staging the resources:

```sh
bun --cwd desktop install --frozen-lockfile
bun run dev
```

Browser preview displays a native-runtime-unavailable state; enrollment and worker
controls require Tauri. Package output is under `desktop/src-tauri/target/release/bundle/`.

The desktop app checks the public worker GitHub releases on startup and every six
hours. A newer stable version with a desktop package for the current OS and
architecture shows a dismissible notice linking to its release page. Install
updates manually; the check does not download packages or stop jobs. Drafts,
prereleases, engine releases and Docker-only releases are ignored. Offline or
rate-limited checks stay quiet. This requires publishing desktop packages as
GitHub Release assets; temporary Actions artifacts do not trigger a notice.

## Headless native worker

For a headless native source checkout, install the engines through their upstream
channels. The setup helper prepares the supporting runtime and native binaries;
it does not install engines. Then use the management CLI:

```sh
bun scripts/setup.ts
bun scripts/worker.ts preflight
WORKER_CONTROL_URL=https://guerrilla.dad bun scripts/worker.ts enroll --token-stdin
bun scripts/worker.ts run
```

`dist/guerrilla-worker` and `dist/guerrilla-pipeline` are native binaries. The
CLI also supports `start`, `status`, `stop`, `drain`, and `disconnect` for a
background worker, with `--config /absolute/path/profile.json` to select a profile.
Set `SPIRULA_BIN`, `LICHTFELD_BIN`, and `LICHTFELD_DENSIFICATION_PLUGIN` to your
installed engines/plugin when using environment-based configuration.

Credentials and writable state stay outside the checkout/image. Enrollment tokens
travel through stdin; credentials and signed transfer URLs never belong in logs.
The agent needs no database, storage-provider credentials, inbound ports, or Docker
socket. One agent executes one scan at a time and requires one visible hardware
GPU. Agents using the same GPU must share their supervisor and pipeline lock paths.

## Linux Docker image

The Linux NVIDIA image includes built LichtFeld and Spirula engines, the compatible
LichtFeld densification plugin, reconstruction models, and all supporting runtimes.
It needs no host engine installation or engine bind mounts. This differs from the
desktop app, which uses engines installed by the owner.

Build the combined runtime and Rust worker layers using Bun and Docker Buildx:

```sh
bun install --frozen-lockfile
bun run build:docker
```

The build helper downloads and verifies checksum-locked LichtFeld and Spirula
artifacts at build time. LichtFeld's artifact includes its built libraries and
resources; the worker build does not compile it again. Default image tags are
`guerrilla-runtime:local` and `guerrilla-worker:local`. Override them with
`bun run build:docker --runtime-image TAG --worker-image TAG`. The resulting image
contains the engines and model assets needed by both workflows, without a
first-job engine or model download.

The host still needs an NVIDIA driver, NVIDIA Container Toolkit, and a CPU exposing
the x86-64-v3 features required by the bundled LichtFeld artifact. Enroll once
using a persistent credential/state directory; paste the token into stdin and
press Enter:

```sh
docker run --rm -i --gpus device=GPU_UUID \
  -v /host/worker-state:/state \
  -e WORKER_CREDENTIAL_FILE=/state/credentials.json \
  -e WORKER_CONTROL_URL=https://guerrilla.dad \
  guerrilla-worker:local enroll --token-stdin
```

Run the foreground worker with one visible GPU and persistent scratch/lock paths:

```sh
docker run --rm --gpus device=GPU_UUID \
  -v /host/worker-state:/state \
  -v /host/worker-scratch:/scratch \
  -v /host/shared-gpu-locks:/gpu \
  -e WORKER_CREDENTIAL_FILE=/state/credentials.json \
  -e WORKER_SCRATCH=/scratch \
  -e WORKER_CONTROL_URL=https://guerrilla.dad \
  guerrilla-worker:local run
```

Use the same server URL for enrollment and execution. Enrollment also performs
GPU preflight, so expose the same single GPU in both commands. Make these host
directories writable by the container's worker account. Share the
same host `/gpu` bind directory across all workers/worktrees using a GPU; separate
container volumes cannot provide host exclusion. Both supervisor and pipeline
locks use that directory. No ports or Docker socket are exposed. Credentials,
scratch checkpoints, and GPU locks are the only required host data mounts.

`Dockerfile.engine` and its artifact tooling remain source-build references for
the LichtFeld artifact consumed by the current runtime. The previous automatic
artifact/image publication recipes are retained in
[release-process notes](docs/release-process.md); the current helper builds local
images without publishing them.

## Durability and compatibility

The worker preserves successful stage outputs and the latest verified resume
checkpoint. Replacement outputs become durable before superseded data is pruned.
Lease loss stops execution; cancellation cleanup requires the server's explicit
cancel authorization. Startup recovery precedes claiming new work. Gaussian limits
are enforced and enrolled SOG exports are verified locally. There is no public
SuperSplat publication integration.

Checkpoint compatibility requires matching input, settings, engine builds, and
runtime identities; a matching worker protocol alone does not establish it.

## Checks

```sh
bun run check
bun run check:python
bun run test
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets -- -D warnings
bun run test:rust
bun run test:python
python -m unittest discover -s tests -p 'test_*.py'
bun --cwd desktop run check
bun --cwd desktop run test
bun --cwd desktop run build
docker build --target test -t guerrilla-worker:test .
```

One CI workflow checks Windows, Linux, and macOS on each push to `main`.
Native desktop packages are built by the manual
`packages` workflow input and uploaded as artifacts. The workflow does not publish
releases or bundle engines into desktop packages. The separate `build:docker`
command bundles engines into the Linux image. CPU/mock checks are not GPU scan
acceptance; validate complete engine workflows and resume on the target GPU before
claiming a release's GPU acceptance.

## License

Original worker code is GPL-3.0-only; the separately versioned protocol is MIT.
Bundled dependencies retain their own terms. Desktop engine installations are
external; the Docker image includes engines under their upstream terms.
Existing RoMa/DINOv3 model terms are in [docs/model-terms.md](docs/model-terms.md).
See [dependency review](docs/dependency-review.md) for redistribution evidence and
remaining release work. Report security issues privately to the repository owner.
