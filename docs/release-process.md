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

The next section records the previous automatic release workflow as historical
reference. Its publication behavior is not active. The later Linux engine artifact
and runtime sections retain useful artifact/source-build references; consuming the
pinned LichtFeld artifact is part of the current Docker build.

# Historical release preparation and gates

## Shared GitHub Actions run (previous behavior)

Before the cross-platform migration, `.github/workflows/windows-release.yml` was
displayed as **Worker release** and started once per push to `main` or manual
dispatch. Windows and Linux ran as parallel jobs with the same commit and run
number. Each job checked and published its own platform independently; a Linux
failure did not block a successful Windows installer release. The existing Windows workflow path is
retained to continue its run counter and avoid reusing installer version tags.

## Windows tray installer

The owner approved automatic Windows installer publication on each push to
`main` in `pstoebenau/guerrilla-worker`. The protocol is now MIT licensed.
`.github/workflows/windows-release.yml` builds an unsigned, per-user Windows x64
installer, runs agent checks and application/install/uninstall smoke tests, and
publishes a GitHub Release with matching source, build identity and SHA-256 sums.
Versions use the package major/minor and GitHub run number (`windows-v0.1.N`).
Reruns preserve an already published release. Each push builds its tip commit;
a push containing multiple commits produces one release. Failing checks prevent
publication. No cancellation of older builds is configured.

Only the tray application, worker source, protocol, Node and .NET runtimes are
included. No Python, engines, plugins, CUDA libraries, weights or scans are
redistributed. Notices for the exact bundled runtimes are included by the build.
CI does not claim GPU scan acceptance. Updates are manual; signing is intentionally
not configured. The desktop application works without Bun, Node or .NET installed.

## Docker image (separate review)

The owner approved automatic Linux releases on every push to worker `main`.
The Linux job in the shared workflow also supports manual runs on `main`; it no longer
requires `RELEASE_APPROVED`, a pre-existing version tag, or environment approval.
Each push gets a `linux-vMAJOR.MINOR.RUN_NUMBER` GitHub Release and a
`ghcr.io/pstoebenau/guerrilla-worker:MAJOR.MINOR.RUN_NUMBER` image tag.
Image tags use the version alone. GitHub Release tags retain the platform
prefix so Windows and Linux can publish separate releases for the same version.
Windows and Linux now share the workflow run number. Linux releases do not
replace the latest Windows installer release used by the tray app. Completed release reruns
preserve their assets; newer pushes do not cancel older builds.

GPL-3.0-only is accepted for worker code and MIT for the protocol. The owner
approved release with the upstream terms, including the RoMa MIT assessment and
separate DINOv3 agreement. The previous blanket legal attestation gate has been
replaced by checksummed document packaging (legal/manifest.json version 2).

Each release runs source/CPU checks, packages worker and protocol sources,
collects pinned upstream sources including submodules, and builds the image.
Before pushing, it exports installed binary notices and the LichtFeld builder's
dependency source trees, downloads, patches/ports and build settings. Missing
required documents, changed checksums, failed source downloads, failed builds or
missing builder sources stop publication.

The registry image receives SBOM and provenance attestations. Matching source,
notice, dependency-build and runtime-inventory assets accompany the GitHub
Release. Dependency archives are split into numbered parts; concatenate in
filename order before extracting. The inventory contains SHA-256 checksums.
See linux-release-review.md for the scope and how to obtain distribution sources.
CI does not claim Linux GPU scan acceptance or blanket legal certification.

The release gate checks concrete contents rather than requiring a human to
certify every transitive package. Third-party licenses remain their own; source
and notice access must be maintained for recipients of published images.


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
