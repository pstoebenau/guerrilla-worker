# Packaged license documents

The owner accepted GPL-3.0-only for worker code, MIT for the protocol, and the
third-party terms recorded in [model terms](../docs/model-terms.md).

`manifest.json` records SHA-256 checksums for upstream notices. Release packages
must preserve these exact documents, the worker LICENSE and dependency notices.
Checksums establish document integrity; they do not replace license obligations.

Spirula, RoMaV2/DINOv3, Node, FFmpeg and converter dependencies retain their own
terms. Desktop runtime downloads use the same Spirula/model pins as containers.
See [source and distribution constraints](../docs/linux-release-review.md).
