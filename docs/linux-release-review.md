# Release licensing constraints

The owner accepted GPL-3.0-only for worker code and MIT for the protocol, and
accepted the recorded RoMa/DINOv3 interpretation and model terms on 2026-10-08.
RoMa's MIT grant applies to its author's contributions; it does not replace
DINOv3's agreement. Preserve both complete notices.

Spirula v2026.10.16 uses RoMaV2 v2.0.1 (`romav2.0.1.pt`), released from commit
`95c9968145c8906b7b59383258e9f73b02853d89`. The checkpoint SHA-256 is
`1557dec0d21b62366465f7ff4d5fdf228cc695d0582e196ad2b80e05230828b7`.
The DINOv3 terms revision remains `adc254450203739c8149213a7a69d8d905b4fcfa`.

`scripts/source_bundle.py` collects pinned upstream sources and recursive
submodules outside the checkout. Native package source archives include Rust,
Svelte, build recipes and lockfiles. The container `release-evidence` target
collects installed notices and OS package versions from the actual worker
layers. Review corresponding source for redistributed binaries and linked
libraries; snapshots alone do not assert completeness.

Package validation, actual GPU acceptance and public publication are separate
gates. Keep license notices and distribution evidence with each release.
