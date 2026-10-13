# Current cross-platform builds

The current worker uses Rust execution and the Tauri management app. Build desktop
packages with `bun run build` on each target OS. Desktop packages include
supporting dependencies but use LichtFeld, Spirula, and the densification plugin
installed by the owner. They do not build, download, or bundle engine binaries.

The Linux Docker image is fully self-contained: it includes built LichtFeld and
Spirula, the compatible densification plugin, model assets, and supporting
runtimes. Build it locally with Bun and Docker Buildx:

```sh
bun install --frozen-lockfile
bun run build:docker
# Optional custom tags:
bun run build:docker --runtime-image guerrilla-runtime:dev --worker-image guerrilla-worker:dev
```

The helper uses the existing public Linux LichtFeld build pinned by
`packaging/docker-engines.lock.json`. It checks the archive, metadata, and checksum
manifest against the pinned recipe and x86-64-v3 platform baseline, then passes a
named artifact context to `Dockerfile.runtime`. The runtime also downloads its
checksum-pinned upstream Spirula release, compatible plugin, and model assets at
build time. `Dockerfile` adds the Rust worker and pipeline without recompiling
engines. Runtime and worker image defaults are `guerrilla-runtime:local` and
`guerrilla-worker:local`; the helper never publishes images or releases.

The Docker host needs a compatible NVIDIA driver and NVIDIA Container Toolkit,
plus an x86-64-v3-capable CPU for the bundled LichtFeld artifact. No host engine
installation or mount is needed. Only credentials/state, scratch checkpoints,
and shared GPU lock directories are mounted; see [build and run examples](../README.md#linux-docker-image).
GPU preflight and complete scan/resume validation remain necessary on the target
hardware. A source/CPU build does not establish GPU acceptance.

The checks workflow does not build LichtFeld or download Spirula; its optional
**desktop** packages are uploaded as CI artifacts. Existing license,
dependency-source, and model-term review remains applicable before public
distribution, including the engines bundled into Docker. See
[cross-platform architecture](cross-platform-worker.md).

## Desktop release

The cross-platform workflow is `.github/workflows/worker.yml`, displayed as
**Worker checks and desktop packages**. Pushes to `main` run checks only.
To build desktop distributions, select **Run workflow** on `main` and enable
`packages`, or run from this repository:

```sh
gh workflow run worker.yml --ref main -f packages=true
gh run list --workflow worker.yml --branch main --event workflow_dispatch --limit 1
```

Use the returned run ID for `gh run watch RUN_ID --exit-status`. Once it passes,
download its Windows x64, Linux x64, and macOS Apple Silicon/Intel artifacts:

```sh
gh run download RUN_ID --dir dist/release
```

Windows and Linux package runners require at least 70 GiB free; macOS requires
12 GiB. Use `windows_package_runner`, `linux_package_runner`,
`macos_package_runner`, and `macos_intel_package_runner` to choose larger or
dedicated runner labels when needed.
Each label must select the correct OS with its native build tools. The Windows
installer smoke test also requires a fresh worker account. macOS packages have
separate Apple Silicon (`macos-15`) and Intel (`macos-15-intel`) jobs.

Package builds upload Actions artifacts, not GitHub Releases. Artifacts expire
after 14 days. To publish them:

1. Choose a version and commit it on `main` before dispatching the package build.
   Keep `package.json`, `desktop/package.json`, `desktop/src-tauri/tauri.conf.json`,
   and `desktop/src-tauri/Cargo.toml` in sync, and refresh the desktop Cargo lockfile.
2. Create one cross-platform release tag, such as `v0.3.0`, targeting the exact
   commit from the successful package run. A draft lets you upload and review all
   packages before publishing:

   ```sh
   gh release create v0.3.0 --target BUILD_COMMIT_SHA --draft \
     --title 'Guerrilla Worker 0.3.0' --generate-notes
   gh release upload v0.3.0 /absolute/path/to/package /absolute/path/to/checksums
   ```

3. Upload the platform packages and their checksums, notices, and matching source
   evidence. Upload every Windows `.bin` payload slice with its setup executable;
   a setup executable alone is incomplete. Repeat the upload command for the
   remaining assets. GitHub provides worker source archives for the release tag;
   preserve the bundled dependencies' corresponding source and notices too.
4. Review the assets and release notes, then publish:

   ```sh
   gh release edit v0.3.0 --draft=false --latest
   ```

GitHub limits each release asset to [under 2 GiB](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
The Windows installer already spans files below this limit. Check Linux/macOS
package sizes before uploading; oversized packages need another download host or
split files with reassembly instructions. The desktop update notice currently
recognizes complete `.AppImage`, `.deb`, `.rpm`, `.dmg`, `.app.tar.gz`, and Windows
installer assets, not split-only packages or external download links.

The app's update notice requires a published stable release with a higher version
and a desktop asset matching the user's OS and architecture. Drafts, prereleases,
Actions artifacts, and source-only releases do not trigger it. Installation is
manual. Docker image publication remains separate from desktop releases.

## What CI verifies

Compilation verifies code and linking. Behavioral tests verify checkpoint retention,
resume, leases, cancellation, profile isolation, and update selection; these can
fail even when the binaries build successfully. Python checks cover helpers that
Rust compilation does not validate. The Linux CPU image job checks the container
build and its tests.

The Windows installer smoke test runs only for manual package builds. It installs
the distribution, checks bundled files, runs the installed worker's status and
preflight commands, and uninstalls while verifying that the isolated profile and
checkpoint survive. Installer compilation alone cannot establish those behaviors.
Native desktop `cargo test` also compiles the app, so a separate preceding
`cargo check` is unnecessary.

# Linux engine artifact and build references

The current helper consumes the verified binary artifact described below. It does
not run the previous recipe-discovery, automatic compilation, or publication jobs.
Source-build commands remain available when maintaining the engine artifact.

## Reusable Linux engine artifact

The Linux engine/runtime Docker targets use CUDA 12.8.1 on Ubuntu 24.04 from
NVIDIA's
`nvcr.io/nvidia/cuda` registry. This uses the same CUDA images without depending
on Docker Hub's CUDA pull quota and requires no additional CI credentials.

The previous Linux release workflow resolved a checksummed file artifact in a
GitHub Release tagged `lichtfeld-linux-recipe-<sha256>`. The recipe hashes
`Dockerfile.engine`, `scripts/build_source_bundle.py`,
`scripts/package_engine_artifact.py`, `scripts/check_cpu_baseline.py` and
`patches/lichtfeld-cpu-baseline.patch`. Worker changes do not invalidate it.
In that workflow a missing recipe was compiled on a separate hosted runner and a
complete published recipe was reused. The current helper requires its pinned
artifact; missing or mismatched assets fail the build instead of triggering
compilation.

The builder exports `lichtfeld-linux-amd64.tar.gz`, containing the executable,
Python module, shared libraries (including OpenMesh) and resources under a
single `lichtfeld/` directory. It also exports the matching patched LichtFeld
source in numbered `lichtfeld-source.tar.gz.part*` assets, dependency source
archives and build inventories. `engine-artifact.json` records the recipe,
platform, CPU baseline and archive SHA-256. `SHA256SUMS.txt` covers every asset.
The compiler runs in Docker for reproducibility, but no LichtFeld image is
published or used to deliver the engine.

The previous publication process released the artifact only after all uploads
completed and preserved completed artifacts on reruns. It did not mark engine
releases Latest. Keep the existing GitHub Release assets available for rebuilds
and corresponding-source access.

The current build helper downloads the archive, metadata, and checksum manifest
using the URLs and SHA-256 values in `packaging/docker-engines.lock.json`, verifies
all three, and supplies a named Docker build context. Docker checks the expected
archive SHA-256 again before extracting into `/opt/lichtfeld`. A build-time bind
mount keeps the compressed archive out of runtime layers. The matching source
evidence remains on the pinned artifact release; publication must preserve access
to it. The runtime retains the artifact metadata, checksum manifest, and release URL
under `/opt/lichtfeld/artifact-metadata`. There is no LichtFeld registry
login or engine image digest in this path.

Linux engine C/C++ code, CUDA host code and vcpkg C/C++ builds target the fixed
`x86-64-v3` CPU baseline, with generic tuning. This enables AVX2/FMA and
LichtFeld's optimized CPU paths on modern Intel and AMD CPUs, including Ryzen 5
3600. CPUs must expose all x86-64-v3 features, including when running in a VM.
The pinned upstream `-march=native` is patched out even when `BUILD_PORTABLE=ON`,
so AVX-512 build hosts cannot raise the CPU requirements. Configuration rejects
generated engine commands that require higher instruction sets before compilation.
GPU architecture settings are unchanged.

This baseline applies to code compiled by the engine recipe; separately bundled
Spirula, Python wheels, system libraries and NVIDIA drivers retain their own
runtime requirements. Rebuild both
the engine artifact and the worker image when changing the baseline; restarting
or rebuilding only the worker with the old engine artifact keeps the incompatible
library. The source evidence archive includes `src/lichtfeld/guerrilla-build.patch`
and the generated compile commands from the actual build.

The engine build cleans vcpkg build trees and package staging after each port,
while retaining downloads for source evidence. After installation it removes
object files, packages the evidence, then deletes remaining build intermediates
in the same Docker layer. The compiler and dependency build tree never enter
the artifact. A new engine artifact still requires compilation and validation on
a suitable build machine; the current worker build reuses the existing artifact.

For maintainers creating a replacement artifact from source (not required by
`bun run build:docker`):

```sh
recipe=$(sha256sum Dockerfile.engine scripts/build_source_bundle.py scripts/package_engine_artifact.py scripts/check_cpu_baseline.py patches/lichtfeld-cpu-baseline.patch | sha256sum | cut -d ' ' -f 1)
docker buildx build --platform linux/amd64 -f Dockerfile.engine --target engine-artifact --build-arg ENGINE_RECIPE="$recipe" --output type=local,dest=dist/lichtfeld .
python3 scripts/package_engine_artifact.py verify dist/lichtfeld
sha=$(sha256sum dist/lichtfeld/lichtfeld-linux-amd64.tar.gz | cut -d ' ' -f 1)
metadata_sha=$(sha256sum dist/lichtfeld/engine-artifact.json | cut -d ' ' -f 1)
checksums_sha=$(sha256sum dist/lichtfeld/SHA256SUMS.txt | cut -d ' ' -f 1)
docker buildx build --platform linux/amd64 --load -f Dockerfile.runtime --target combined-runtime \
  --build-context lichtfeld-artifact=dist/lichtfeld \
  --build-arg LICHTFELD_SHA256="$sha" \
  --build-arg LICHTFELD_METADATA_SHA256="$metadata_sha" \
  --build-arg LICHTFELD_CHECKSUMS_SHA256="$checksums_sha" \
  --build-arg LICHTFELD_RECIPE="$recipe" \
  --build-arg LICHTFELD_RELEASE_URL=local-source-build \
  -t guerrilla-runtime:local .
docker build --platform linux/amd64 --target worker --build-arg WORKER_RUNTIME_IMAGE=guerrilla-runtime:local -t guerrilla-worker:local .
```

To consume a published build, use `gh release download <engine-tag>` instead
of compiling, verify its checksums, then pass that directory and the expected
archive/metadata/manifest SHA-256 values, recipe, and release URL to the runtime
build. Use `bun run build:docker` for the currently pinned published artifact. The named context binds only the binary
archive; it does not copy source archives into the runtime. The CPU-only `test`
target requires neither the engine artifact nor a prebuilt runtime.

## Reusable Linux runtime and worker builds

`Dockerfile.engine` is the source-build recipe for the LichtFeld Linux artifact
and its matching evidence. The current helper downloads the checksummed GitHub
Release archive, carrying the executable, Python module, resources, and shared
libraries needed together. There is no LichtFeld compiler in the worker build.

`Dockerfile.runtime` installs the slow runtime dependencies. Its Spirula download
and LichtFeld branches are independent. `spirula-runtime` contains Spirula and
the shared converter without LichtFeld. `lichtfeld-runtime` contains the LichtFeld
workflow without Spirula. `combined-runtime` assembles
both and is the current helper's default configuration.

The previous runtime job looked up a content-addressed artifact in
`ghcr.io/pstoebenau/guerrilla-worker-runtime`. The recipe contains only
`Dockerfile.runtime`, both Docker requirements files, the converter package and
lockfile, and the resolved LichtFeld archive SHA-256. No worker, protocol, pipeline
Python file, notice or documentation change invalidates that artifact.
Registry BuildKit caching preserves unchanged engine branches when a runtime
input changes. The previous release recorded `runtime-image.txt` alongside
`engine-artifact-reference.json` and consumed the runtime's immutable digest.
The current helper builds a local runtime tag and reuses Docker build caching.

`Dockerfile` compiles the Rust worker and pipeline independently, then copies
them, Python helper sources, and notices onto the prepared runtime. Worker and
pipeline changes can reuse cached engine/runtime layers. Engines, models, and
runtime dependencies are prepared when building `Dockerfile.runtime`; executing
`Dockerfile` with an existing runtime tag does not download or compile engines.
The current checks workflow does not publish versioned worker images.

For a Spirula-only worker, skip the LichtFeld build entirely:

```sh
docker build --platform linux/amd64 -f Dockerfile.runtime --target spirula-runtime -t guerrilla-runtime:spirula .
docker build --platform linux/amd64 --target worker --build-arg WORKER_RUNTIME_IMAGE=guerrilla-runtime:spirula -t guerrilla-worker:spirula .
```

Use `lichtfeld-runtime` instead for a LichtFeld-only runtime. Single-engine
workers advertise only their available engine. To use an existing combined
runtime, pass
`--build-arg WORKER_RUNTIME_IMAGE=ghcr.io/pstoebenau/guerrilla-worker-runtime@sha256:<digest>`
to the worker build. The `release-evidence` target exports installed image notices
and runtime inventories. The previous release job attached verified engine source
files directly from the engine artifact release; maintain corresponding-source
access when publishing a new image.

All SOG and SPZ conversions use `@playcanvas/splat-transform@3.10.1`, installed
with `npm ci` from a separate lockfile. SPZ is explicitly written as version 3
for compatibility with the existing gzip/CRC/count validator. SOG count/cap
validation and PPISP sidecars are preserved. Current desktop packages and the
native setup helper vendor the converter; the Linux runtimes include it too.
