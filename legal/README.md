# Packaged license documents

The owner accepts GPL-3.0-only for original worker code, MIT for the protocol,
and the upstream third-party terms described in docs/model-terms.md and
docs/dependency-review.md. The unlicensed COLMAP UI plugin has been removed.

manifest.json version 2 records exact SHA-256 checksums of upstream license
documents. It verifies packaging integrity; it does not assert blanket legal
certification. Release builds require the worker LICENSE, the manifest and all
matching documents. Version 1 manifests remain readable for compatibility.

The manifest includes COLMAP, RoMaV2, DINOv3, LichtFeld, Spirula, densification
and Node. CI additionally exports binary notices, pinned source snapshots and
dependency source/build material from the exact image build as release assets.
See docs/linux-release-review.md for their scope and source access instructions.

The Windows installer uses installed engines/Python rather than redistributing
them, and carries separate notices for its bundled Node and .NET runtimes.
