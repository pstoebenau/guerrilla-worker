# Guerrilla Worker

Guerrilla Worker runs Spirula scans locally or as an enrolled worker for a
Guerrilla server. It supports Windows x64, Ubuntu x64 and Apple Silicon macOS.
Spirula uses native Vulkan, with MoltenVK on macOS. A compatible hardware GPU
and its operating-system driver are required.

## Install & connect

Download your platform's package from
[Releases](https://github.com/pstoebenau/guerrilla-worker/releases/latest).
Windows uses a single setup EXE, Linux offers AppImage/DEB/RPM, and macOS offers
an arm64 DMG. Copy the Mac app into Applications before opening it.

In Setup, choose **Install / repair runtime** to download and verify Spirula
v2026.10.16 and its RoMaV2 reconstruction model. The initial download is about
1.2 GB. Runtime setup also runs automatically before scanning or starting a worker;
verified installations work offline. Node, FFmpeg and the SOG/SPZ converter
are included in the desktop package.

Paste an enrollment token from your server's Workers page, choose **Enroll
worker**, then **Start worker**. Enrollment does not require a ready GPU.
An offline personal worker's jobs stay queued; they do not fall back to paid GPUs.
Credentials and scratch/checkpoint data stay outside the installation folder.
Closing the window keeps the worker in the tray; use **Quit and stop all jobs**
to end it. Uninstall preserves profile data and checkpoints.

The Mac package uses ad-hoc signing. After the first launch attempt, macOS may
require **System Settings → Privacy & Security → Open Anyway**. Developer ID
notarization is not configured.

## Local scans

From a source checkout, install Bun and Rust, then prepare the native worker:

```sh
bun install --frozen-lockfile
bun scripts/setup.ts
bun scripts/worker.ts scan /absolute/path/video.mp4 --output /absolute/path/result --max-cap 1000000
```

An image folder or public video URL can replace the video path. Dense
initialization is enabled by default with Spirula's Balanced preset. Use
`--no-densification` to skip it, or `--settings /absolute/path/settings.json`
for supported settings. SOG, SPZ and a local preview are written to the output.
SuperSplat publication is not supported.

After interruption, rerun with `--resume` and the same input, output, Gaussian
limit and training settings. Completed stages and the latest usable training
checkpoint are retained. Never increase a requested `--max-cap` implicitly.

## Headless enrollment

```sh
bun scripts/worker.ts setup-runtime
bun scripts/worker.ts enroll --token-stdin
bun scripts/worker.ts preflight
bun scripts/worker.ts start
bun scripts/worker.ts status
```

Use `--config /absolute/path/profile.json` before the command for an isolated
profile. Tokens are entered on stdin and are never saved in project files.
Workers use outbound protocol v2 and scoped transfers, without database,
bucket or cloud-provider credentials.

## Linux Docker image

The Linux amd64 image includes the same pinned Spirula release and RoMaV2
weights. The host needs an NVIDIA driver and NVIDIA Container Toolkit with
Vulkan support. No Docker socket or child containers are used.

```sh
bun run build:docker
mkdir -p /absolute/worker/credentials /absolute/worker/scratch /absolute/worker/gpu
docker run --rm -it --gpus device=0 \
  --mount type=bind,source=/absolute/worker/credentials,target=/credentials \
  --mount type=bind,source=/absolute/worker/scratch,target=/scratch \
  --mount type=bind,source=/absolute/worker/gpu,target=/gpu \
  -e WORKER_CONTROL_URL=https://guerrilla.dad \
  -e WORKER_CREDENTIAL_FILE=/credentials/worker.json \
  -e WORKER_SCRATCH=/scratch guerrilla-worker:local enroll --token-stdin
```

Use the same mounts and environment for `run`. Builds stay local unless
`bun run build:docker --push` is given explicit registry image tags.

## Development & release

`bun scripts/development-worker.ts` builds immutable snapshots of the Rust
worker and pipeline for native development. Linux development uses the prepared
container. The parent Guerrilla app's `bun dev --worker` delegates here.

Build desktop packages natively on each target OS with `bun run build`.
Build hosts need Bun, Rust, Python 3 for packaging, and Tauri's platform
prerequisites; installed workers need none of those development tools.
Run `bun run check`, `bun run test`, `bun run test:rust`, and the desktop's
`bun run check`. Packaging checks use `python3 -m unittest discover -s tests`.

Public release, actual GPU acceptance, and deployment are separate gates.
See [release constraints](docs/release-process.md), [model terms](docs/model-terms.md),
and [license evidence](docs/linux-release-review.md). Preserve root `scans/`.
