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
`ghcr.io/pstoebenau/guerrilla-worker:linux-MAJOR.MINOR.RUN_NUMBER` image tag.
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

The Linux release workflow first resolves a LichtFeld engine artifact in
`ghcr.io/pstoebenau/guerrilla-lichtfeld`. Its recipe tag is the SHA-256 of the
checksums of `Dockerfile.engine`, `scripts/build_source_bundle.py`,
`scripts/check_cpu_baseline.py` and `patches/lichtfeld-cpu-baseline.patch`.
Changes to worker code do not invalidate it. Changing any engine input builds
a new artifact automatically, on a separate hosted runner before worker packaging.
The tag is a lookup key; the worker receives the resolved `sha256` image digest,
and records that reference in its release asset `engine-image.txt`.

The artifact contains `/opt/lichtfeld` (including the OpenMesh library required
by the Python module) and `/release-evidence`. The latter carries the dependency
source archives, port patches and inventory from the same build. Worker releases
still attach this evidence and collect pinned upstream source snapshots. Keep
engine artifacts available in GHCR for repeat builds; deleting one causes its
recipe to compile again. Concurrent first-time releases can compile the same
recipe independently; each worker uses the digest resolved by its engine job.

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

For a local source build:

```sh
docker build -f Dockerfile.engine -t guerrilla-lichtfeld:local .
docker build --target worker -t guerrilla-worker:local .
```

To consume a published engine instead, authenticate to GHCR as needed and pass
`--build-arg LICHTFELD_IMAGE=ghcr.io/pstoebenau/guerrilla-lichtfeld@sha256:<digest>`
to the worker build. The CPU-only `test` target does not require the engine.
