# Distribution constraints

Spirula v2026.10.16 is pinned to source commit
`65246c5579c127081bff6f4422bd12a542906f78` and official Vulkan release archives.
Desktop runtime setup verifies archive and RoMa checkpoint SHA-256 digests before
installation. Model terms are recorded in [model-terms.md](model-terms.md).

Converter dependencies are pinned by the npm lockfile; Node and FFmpeg archives
are pinned in `packaging/runtime-lock.json`. The Rust and desktop lockfiles pin
native dependencies. Preserve upstream notices when distributing their binaries.

Runtime setup and package builds do not establish GPU acceptance. Test the
complete workflow on the actual target GPU before claiming support for that
hardware. Publication requires accepted licensing and reviewed contents.
