# Release preparation and gates

Only fresh public repository history may be used. Do not initialize its remote,
push tags, or dispatch publication until the owner accepts a license and the
review below passes. GPL-3.0-only has now been accepted for worker code; protocol
licensing and third-party redistribution review remain pending. The approved future destinations are
`pstoebenau/guerrilla-worker` and `ghcr.io/pstoebenau/guerrilla-worker`.

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
4. Create fresh public history, tag the accepted source, and enable a protected
   GitHub environment named `release` with required human reviewers. Set repository
   variable `RELEASE_APPROVED=true` only after license and contents review. The
   prepared workflow is manual, requires the correct public repository, a tag
   matching package version, LICENSE, and this variable. It does not run on push.
5. Dispatch release.yml on the version tag. Review image SBOM and provenance
   attachments, source/protocol archives, notices and release notes. Record the
   registry manifest digest from build-metadata.json; a local image ID is not a
   registry manifest digest. Pin the platform to that digest and protocol archive
   checksum only after clean consumption tests.

The workflow uses a Docker container builder, max provenance, and SBOM attestations.
[Docker's attestation documentation](https://docs.docker.com/build/ci/github-actions/attestations/)
explains that pushed images support these attestations; a locally loaded image
does not prove registry provenance was published. Build arguments must never carry
secrets. Never run the release workflow from the private platform repository.

No release job has been dispatched. The local candidate image ID is
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
