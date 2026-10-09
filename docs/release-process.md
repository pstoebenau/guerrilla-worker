# Release preparation and gates

## Shared GitHub Actions run

`.github/workflows/windows-release.yml` is displayed as **Worker release** and
starts once per push to `main` or manual dispatch. Windows and Linux run as
parallel jobs in that run, with the same commit and run number. Each job checks
and publishes its own platform independently; a Linux failure does not block a
successful Windows installer release. The existing Windows workflow path is
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


## Reusable Linux engine artifact

All Linux Docker targets pull CUDA 12.8.1 on Ubuntu 24.04 from NVIDIA's
`nvcr.io/nvidia/cuda` registry. This uses the same CUDA images without depending
on Docker Hub's CUDA pull quota and requires no additional CI credentials.

The Linux release workflow resolves a checksummed file artifact in a GitHub
Release tagged `lichtfeld-linux-recipe-<sha256>`. The recipe hashes
`Dockerfile.engine`, `scripts/build_source_bundle.py`,
`scripts/package_engine_artifact.py`, `scripts/check_cpu_baseline.py` and
`patches/lichtfeld-cpu-baseline.patch`. Worker changes do not invalidate it.
A missing recipe is compiled on a separate hosted runner; a complete published
recipe is reused. Authentication failures and incomplete releases stop the job
instead of silently triggering another compilation.

The builder exports `lichtfeld-linux-amd64.tar.gz`, containing the executable,
Python module, shared libraries (including OpenMesh) and resources under a
single `lichtfeld/` directory. It also exports the matching patched LichtFeld
source in numbered `lichtfeld-source.tar.gz.part*` assets, dependency source
archives and build inventories. `engine-artifact.json` records the recipe,
platform, CPU baseline and archive SHA-256. `SHA256SUMS.txt` covers every asset.
The compiler runs in Docker for reproducibility, but no LichtFeld image is
published or used to deliver the engine.

The artifact release is published only after all uploads complete. Reruns
preserve completed artifacts; an unfinished draft stops reuse so it can be
finished or removed before retrying. Engine releases are never marked Latest,
so they do not replace the Windows installer release. Keep these GitHub Release
assets available for rebuilds and corresponding-source access.

The engine job resolves the binary and checksum-manifest SHA-256 values.
The runtime job downloads the archive and pinned checksum manifest with GitHub
CLI, verifies both, then supplies a named Docker build context. Docker checks
the expected SHA-256 again before extracting into `/opt/lichtfeld`. A bind mount
keeps the compressed archive out of runtime layers. Worker releases attach the
verified engine source assets and `engine-artifact-reference.json`, which records
the engine release URL and both checksums. There is no LichtFeld registry login
or engine image digest in this path.

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
the artifact. The first build still requires compilation and must be validated
on the hosted runner; reuse does not guarantee the first build fits.

For a local source build and artifact export:

```sh
recipe=$(sha256sum Dockerfile.engine scripts/build_source_bundle.py scripts/package_engine_artifact.py scripts/check_cpu_baseline.py patches/lichtfeld-cpu-baseline.patch | sha256sum | cut -d ' ' -f 1)
docker buildx build -f Dockerfile.engine --target engine-artifact --build-arg ENGINE_RECIPE="$recipe" --output type=local,dest=dist/lichtfeld .
python3 scripts/package_engine_artifact.py verify dist/lichtfeld
sha=$(sha256sum dist/lichtfeld/lichtfeld-linux-amd64.tar.gz | cut -d ' ' -f 1)
docker buildx build --load -f Dockerfile.runtime --target combined-runtime --build-context lichtfeld-artifact=dist/lichtfeld --build-arg LICHTFELD_SHA256="$sha" -t guerrilla-runtime:local .
docker build --target worker -t guerrilla-worker:local .
```

To consume a published build, use `gh release download <engine-tag>` instead
of compiling, verify its checksums, then pass that directory and the expected
archive SHA-256 to the runtime build. The named context binds only the binary
archive; it does not copy source archives into the runtime. The CPU-only `test`
target requires neither the engine artifact nor a prebuilt runtime.

## Reusable Linux runtime and worker builds

`Dockerfile.engine` compiles the installed LichtFeld Linux distribution and its
matching source evidence. CI downloads the checksummed GitHub Release archive;
it carries the executable, Python module, resources and shared libraries that
are needed together. There is no LichtFeld compiler in the worker build.

`Dockerfile.runtime` installs the slow runtime dependencies. Its Spirula download
and LichtFeld branches are independent. `spirula-runtime` contains Spirula and
the shared converter, without LichtFeld, PyTorch or RoMa/DINOv3. `lichtfeld-runtime`
contains the LichtFeld workflow without Spirula. `combined-runtime` assembles
both and is the default release configuration.

The runtime job looks up a content-addressed artifact in
`ghcr.io/pstoebenau/guerrilla-worker-runtime`. The recipe contains only
`Dockerfile.runtime`, both Docker requirements files, the converter package and
lockfile, and the resolved LichtFeld archive SHA-256. No worker, protocol, pipeline
Python file, notice or documentation change invalidates that artifact.
Registry BuildKit caching preserves unchanged engine branches when a runtime
input changes. The final release records `runtime-image.txt` alongside
`engine-artifact-reference.json`, and consumes the runtime's immutable digest.

`Dockerfile` bundles TypeScript independently and copies the pipeline and notices
onto the prebuilt runtime. Agent changes rebuild the bundle and final packaging;
pipeline changes recopy the pipeline. Neither triggers engine compilation,
Spirula download, model download, nor runtime package installation. Each push
still runs checks and creates a versioned worker image. A missing runtime recipe
is assembled once; a missing LichtFeld recipe must be compiled again.

For a Spirula-only worker, skip the LichtFeld build entirely:

```sh
docker build -f Dockerfile.runtime --target spirula-runtime -t guerrilla-runtime:spirula .
docker build --target worker --build-arg WORKER_RUNTIME_IMAGE=guerrilla-runtime:spirula -t guerrilla-worker:spirula .
```

Use `lichtfeld-runtime` instead for a LichtFeld-only runtime. Single-engine
workers advertise only their available engine. To use an existing combined
runtime, pass
`--build-arg WORKER_RUNTIME_IMAGE=ghcr.io/pstoebenau/guerrilla-worker-runtime@sha256:<digest>`
to the worker build. The `release-evidence` target exports installed image notices
and runtime inventories; the release job attaches verified engine source files
directly from the engine artifact release.

All SOG and SPZ conversions use `@playcanvas/splat-transform@3.10.1`, installed
with `npm ci` from a separate lockfile. SPZ is explicitly written as version 3
for compatibility with the existing gzip/CRC/count validator. SOG count/cap
validation and PPISP sidecars are preserved. Native installations must install
the converter separately (see README); the Linux runtimes already include it.
