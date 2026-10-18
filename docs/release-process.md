# Release constraints

Build desktop packages natively on Windows x64, Ubuntu x64 and Apple Silicon
macOS with `bun run build`. The build host needs Bun, Rust, Python 3 and Tauri
platform dependencies. Desktop packages bundle Node/FFmpeg and the converter;
Spirula and RoMaV2 install automatically into the owner's data directory.

`bun run build:docker` produces the Linux amd64 runtime and worker images
locally. `--push` requires explicit registry tags and authenticated Buildx.
Docker downloads the same checksum-pinned official Spirula and model assets
as the native installer; the final worker has no Python/CUDA framework runtime.

The worker release workflow builds all three native packages and the matching
Linux image from one commit and numeric version. Publication waits for every
platform and image build, verifies inventories, uploads the complete release
as a draft, then publishes it. Image publication records the immutable digest.
A failed platform must leave the previous public release intact. Retry failed
jobs before their workflow artifacts expire; changed source needs a new build.

Windows distributions are a single setup EXE. Linux distributions include
AppImage, DEB and RPM. macOS distributions include a DMG and app tarball.
Each public asset must remain below GitHub's 2 GiB limit. Keep enrollment tokens,
credentials, environments, scratch and generated runtime assets out of source.

Source archives must include Rust and Svelte source, manifests, lockfiles and
packaging recipes. `scripts/source_bundle.py` collects pinned upstream source
and recursive submodules outside the checkout. The container `release-evidence`
target records installed notices and OS package versions. Preserve upstream
licenses and review corresponding-source obligations before distribution.

Package checks, complete scan/resume validation on the actual worker GPU,
remote acceptance and public deployment are separate gates. Existing owner
license acceptance is recorded in [linux-release-review.md](linux-release-review.md).
