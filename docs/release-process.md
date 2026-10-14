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

The release workflow does not build LichtFeld or download Spirula. It builds
**desktop** packages and publishes them together in one GitHub Release. Existing license,
dependency-source, and model-term review remains applicable before public
distribution, including the engines bundled into Docker. See
[cross-platform architecture](cross-platform-worker.md).

## Desktop release

The cross-platform workflow is `.github/workflows/worker.yml`, displayed as
**Build and release worker**. Every push to `main` builds and tests Windows x64,
Linux x64, macOS Apple Silicon and macOS Intel, then automatically publishes all
four into **one** stable GitHub Release. No manual publication step or Windows-only
release path is required. Manual dispatch on `main` uses the same complete matrix:

```sh
gh workflow run worker.yml --ref main
```

The workflow selects a shared numeric version before packaging. It uses the
version in `package.json` as a minimum, then increments the latest cross-platform
release's patch version when necessary. CI applies that version to both desktop
manifests, the worker/desktop Cargo packages and their lockfiles without creating
an extra commit or push. Reruns reuse a release already allocated to the same
commit. Bump the source version to start a new minor or major series.

Runs on `main` are serialized so version allocation and publication do not race.
A release job waits for **every** native package job, verifies matching versions,
commits and SHA-256 inventories, then creates a draft, uploads every asset,
verifies the completed uploads and publishes it as Latest. A failed platform or
upload leaves the previous public release intact. Rerun failed jobs after a
transient failure; push a fix for code/build failures.

Formats are one self-contained Windows setup `.exe`, Linux AppImage/DEB/RPM,
and macOS DMG and `.app.tar.gz` for both architectures. Linux RPM creation uses
system `rpmbuild` to stream the same filesystem Tauri prepares for the DEB;
this avoids buffering the package filesystem in the Tauri RPM builder's memory.
Linux native builds need `rpm` alongside Tauri's normal system dependencies.
The AppImage bundler receives the base runtime's private wheel library directories in its
build-only library search path so individual ELF files resolve private wheel
dependencies. Driver stubs are excluded from that search path.
macOS DMGs are generated from Tauri's app bundle with `hdiutil makehybrid` and
`hdiutil convert`, without mounting temporary images. This avoids hosted Intel
Mac DiskArbitration detach timeouts while retaining the app and Applications link.

Every new native package is a single file below GitHub's 2 GiB asset limit.
Publication fails if a package grows beyond this limit; new releases never split
packages or Windows installer payloads. GPU libraries and model weights are not
included in the app. **Setup → GPU dependencies** downloads pinned Python, installs
hash-locked binary wheels, verifies library imports and model checksums, then
activates the runtime under the owner's worker data directory. Windows/Linux
first setup needs internet and 25 GiB free. macOS uses its included base runtime.
Engine installations and the NVIDIA driver remain owner-installed.

Archived v0.3.0/v0.3.1 releases contain split Linux packages and Windows slices.
For those older releases, download all matching parts and use the reassembly
helper shipped with that release. New releases need no reassembly.

The release includes a global `SHA256SUMS.txt`, platform runtime manifests/notices,
the original-code license, third-party notices and the tagged worker source.
Apply the release version with `scripts/release_version.py` when reproducing a
build from the source archive. Engine installations and Docker images remain
separate from desktop publication. The app's manual update notice recognizes
both complete native packages and split-package manifests from stable releases.

Windows and Linux package runners require at least 40 GiB free for real GPU setup verification; macOS requires
12 GiB. Manual dispatch accepts `windows_package_runner`, `linux_package_runner`,
`macos_package_runner`, and `macos_intel_package_runner` for larger or dedicated
runner labels. Labels must select the correct OS/architecture and native tools.
The Windows installer smoke test requires a fresh worker account. Package Actions
artifacts remain available for 14 days as build evidence; GitHub Release assets
provide the persistent downloads.

## What CI verifies

Compilation verifies code and linking. Behavioral tests verify checkpoint retention,
resume, leases, cancellation, profile isolation, and update selection; these can
fail even when the binaries build successfully. Python checks cover helpers that
Rust compilation does not validate. Windows/Linux jobs additionally install the optional GPU runtime in an isolated profile and verify that subsequent setup reuses it offline without creating enrollment or scratch. Tests run inside each native release-build job, using release Cargo artifacts and
the packaged Python/Node/converter runtimes. There is no separate check matrix,
repeated frontend build, debug-profile compile, or Docker image publication gate.
The Docker image can be checked independently with the documented local command.

The Windows installer smoke test runs for every release build. It installs
the distribution, checks bundled files, runs the installed worker's status and
preflight commands, and uninstalls while verifying that the isolated profile and
checkpoint survive. Installer compilation alone cannot establish those behaviors.
Native desktop tests run with the release profile after the actual app build,
reusing compiled dependencies. Compilation, behavioral tests and installer smoke
serve different purposes without repeating a separate check-build matrix.

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
