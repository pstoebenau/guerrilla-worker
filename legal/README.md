# Reviewed binary notices

The owner accepted GPL-3.0-only for original worker code. Do not create a reviewed
manifest until the third-party redistribution review and separate protocol license
decision are complete.

Place exact approved third-party license/copyright documents here as `.txt` or
`.md` files, retaining required notices. Create `manifest.json` with version 1,
`redistributionReviewed: true`, and `files: [{"name": "component-LICENSE.txt",
"sha256": "<actual lowercase SHA-256>"}]`. This is a human review attestation,
not a tool-generated legal conclusion. Document corresponding-source delivery
and any separately licensed model requirements in the release review.

Image packaging copies only named, checksummed regular documents (maximum 200,
10 MiB each), plus the explicitly listed worker license/notices/docs. Release
builds require both LICENSE and this manifest; candidates without an accepted
license carry a LICENSE-PENDING marker. No reviewed manifest is supplied now.
