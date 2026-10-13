# Packaged license documents

The owner accepts GPL-3.0-only for original worker code, MIT for the protocol,
and the upstream third-party terms described in docs/model-terms.md and
docs/dependency-review.md. The unlicensed COLMAP UI plugin has been removed.

manifest.json version 2 records exact SHA-256 checksums of upstream license
documents. It verifies packaging integrity; it does not assert blanket legal
certification. Release builds require the worker LICENSE, the manifest and all
matching documents. Version 1 manifests remain readable for compatibility.

The manifest includes COLMAP, RoMaV2, DINOv3, LichtFeld, Spirula, densification
and Node. Docker release tooling can export binary notices, pinned source snapshots
and dependency source/build material from the exact image build. See
docs/linux-release-review.md for their scope and source access instructions.

Desktop packages for Windows, Linux, and macOS use owner-installed engines and
bundle Python, Node, FFmpeg, the export converter, and supporting libraries.
Preserve the notices and corresponding source for those bundled dependencies.
