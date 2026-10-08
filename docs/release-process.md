# Release preparation and gates

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
`.github/workflows/release.yml` also supports manual runs on `main`; it no longer
requires `RELEASE_APPROVED`, a pre-existing version tag, or environment approval.
Each push gets a `linux-vMAJOR.MINOR.RUN_NUMBER` GitHub Release and a
`ghcr.io/pstoebenau/guerrilla-worker:linux-MAJOR.MINOR.RUN_NUMBER` image tag.
Windows and Linux run numbers are independent. Linux releases do not replace the
latest Windows installer release used by the tray app. Completed release reruns
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
