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

GPL-3.0-only is accepted for worker code and MIT for the protocol. The image's
third-party redistribution review remains pending: `legal/manifest.json` is
currently absent. Automatic runs fail with an explicit error until that reviewed
notice bundle is supplied. The following packaging checks remain required:

1. Resolve every distribution blocker in dependency-review.md. Add the accepted
   original-code LICENSE and complete third-party license texts/notices. Include
   exact GPL corresponding sources, submodules, modifications and build scripts
   beside binary downloads; an upstream link alone is not the complete release plan.
   Record reviewed binary notice files and checksums in `legal/manifest.json` as
   described in `legal/README.md`. This manual review attestation is required by
   the release workflow in addition to the original-code license.
2. Review the release.py allowlist and release-inventory.json. Inspect both tracked
   and ignored files, Docker context, archive paths and source. Reject secrets,
   private hostnames/history, customer media, model outputs and personal paths.
3. Run independent checks and hardware tests recorded in release notes. Pin base
   image digests and resolved dependency sources. Generate actual image inventory
   with `python scripts/inventory.py IMAGE OUTPUT_DIRECTORY`; retain the SBOM and
   notices as release attachments, not checked-in generated build artifacts.
4. Push reviewed source to `main` in `pstoebenau/guerrilla-worker`. Both release
   workflows start automatically; checks must pass before publication.
5. Review image SBOM and provenance
   attachments, source/protocol archives, notices and release notes. Record the
   registry manifest digest from build-metadata.json; a local image ID is not a
   registry manifest digest. Pin the platform to that digest and protocol archive
   checksum only after clean consumption tests.

The workflow uses a Docker container builder, max provenance, and SBOM attestations.
[Docker's attestation documentation](https://docs.docker.com/build/ci/github-actions/attestations/)
explains that pushed images support these attestations; a locally loaded image
does not prove registry provenance was published. Build arguments must never carry
secrets. Never run the release workflow from the private platform repository.

The earlier local candidate image ID is
`sha256:49ef94c389f83b6760cf420052f5212ba6ef2f80b659618356c91742890a69c0`
(17,095,306,623 bytes unpacked); it has no RepoDigest because it was not pushed.
Docker SBOM 0.6.0/Syft 0.43 initially rejected Engine 29's minimum API; inventory
generation must report the scanner version and any failure rather than silently
claiming a complete SBOM. A software inventory is not legal clearance.
The API-1.44 retry was interrupted to prioritize GPU acceptance, so the current
review evidence is the actual package inventories; complete SPDX output remains pending.

Future image builds install the explicitly reviewed legal documents beneath
`/usr/share/doc/guerrilla-worker/`. Local no-license candidates carry
`LICENSE-PENDING`; release builds use `REQUIRE_RELEASE_LICENSE=1` and fail without
LICENSE, a reviewed manifest and matching third-party document checksums. The
previously built image ID above predates this packaging change and has not been
rebuilt during GPU acceptance. A legal-document bundle does not itself satisfy
GPL corresponding-source delivery or resolve model restrictions.
