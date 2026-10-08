# Linux binary release evidence

The owner has approved publication under the recorded upstream terms. The
version 2 legal manifest verifies notice checksums; it is not a blanket legal
certification. CI collects the following release contents before publishing.

## Source snapshots

Run with Python 3.12, giving a directory outside the checkout:

```sh
python scripts/source_bundle.py /tmp/guerrilla-release-review/sources
```

This downloads exact commit archives for LichtFeld, Spirula, the densification
plugin, RoMaV2, DINOv3 and vcpkg, plus recursive Git submodules at their recorded
commits. It keeps source archives intact, including their build scripts and
license files. `source-snapshots.json` records SHA-256 values, provenance and
submodule mount paths. Place each submodule snapshot at its recorded mount path
inside its component checkout to reconstruct the source tree. Existing archives
are checked against their saved hashes on reruns. A failed download does not
mark collection complete.

The source snapshots include their original build scripts and notices. CI also
exports lichtfeld-build-dependencies.tar.gz.part* from the actual builder:
vcpkg source trees, downloads, ports/patches, modified triplets, installed package
metadata, CMake dependency source trees and CMakeCache.txt. Concatenate the parts
in filename order and extract; build-source-inventory.json records hashes and
original paths. This preserves the build's selected dependencies instead of
substituting current upstream main branches.

The Spirula release snapshot contains its source and vendored dependencies.
Python wheel license/metadata files are included in the notice archive, retaining
upstream source references for their native libraries. The snapshot inventory
explicitly describes its own limited coverage; it is not a certification of the
entire image.

For Ubuntu binaries, os-packages.tsv records installed versions and corresponding
source package names/versions. Obtain matching source (including Ubuntu patches)
from Ubuntu's source archive by enabling deb-src for the image's Ubuntu 24.04
repositories and running apt-get source SOURCE_PACKAGE=SOURCE_VERSION. Sources
are also indexed at https://launchpad.net/ubuntu/+source/PACKAGE/VERSION and
https://archive.ubuntu.com/ubuntu/pool/. NVIDIA components retain NVIDIA terms;
these are not represented as GPL source packages. Preserve installed notices
and their component-specific source locations alongside these instructions.

## Binary notices

After a candidate build, run:

```sh
python scripts/inventory.py guerrilla-worker:candidate /tmp/guerrilla-release-review/image
```

The existing package/SBOM collection now also saves `binary-notices.tar.gz` from
the actual image. It collects OS copyright/license files, Python wheel notices
and metadata, and notices within engine/plugin/CUDA directories. The collector
uses a network-disabled container and does not modify the image. Its internal
`notice-inventory.json` contains file checksums and unresolved notice findings.
Missing notices are findings, not inferred permissions. An SBOM is complementary
evidence; neither the SBOM nor this archive is an automatic legal approval.

The tracked legal directory also contains the exact top-level license texts for
LichtFeld, Spirula, densification, Node, COLMAP, RoMaV2 and DINOv3. Node's LICENSE
contains its bundled third-party notices. These documents are included in
candidate image notice packaging, before a reviewed manifest exists.

Exact top-level notice sources:

- LichtFeld: https://raw.githubusercontent.com/MrNeRF/LichtFeld-Studio/d8c50c6a3e2273cb74130a6e9023de8d068af52d/LICENSE
- Spirula: https://raw.githubusercontent.com/harry7557558/spirula-studio/1943edaf83abf0d00b9ca2d2023424ba1e831d6a/LICENSE
- Densification: https://raw.githubusercontent.com/shadygm/lichtfeld-densification-plugin/ab0b04e35b12bff65ee87bdaacfa3177c21521d6/LICENSE
- Node: https://raw.githubusercontent.com/nodejs/node/v22.22.0/LICENSE

## RoMa license assessment

On 2026-10-08, GitHub's release API reports the same `romav2.pt` SHA-256 as our
Dockerfile. The `weights` tag resolves to
`ac25bcede24b11975013a4c085baf8c79559cf47`. Its release description supplies no
weight-specific license. The repository supplies the MIT license and its README
explicitly excepts DINOv3. In issue #49, the author also confirms that fine-tuning
RoMaV2 is permitted; the inference assertion is a testing safeguard.

For this release review, we interpret the repository's MIT grant as covering
the author's RoMaV2 code and released model contributions, while retaining the
explicit DINOv3 exception. This is an assessment of the upstream materials, not
a claim that the issue comment expressly discusses checkpoint redistribution.
The project owner accepts this interpretation. A separate upstream confirmation
of the RoMa-owned weights is no longer a publication prerequisite; no question
has been posted. Preserve both the RoMa MIT notice and the complete Meta
agreement for DINO materials. The MIT assessment does not relicense DINOv3 or
replace the remaining image-specific notice/source review.

Evidence:

- https://api.github.com/repos/Parskatt/RoMaV2/releases/tags/weights
- https://github.com/Parskatt/RoMaV2/blob/ac25bcede24b11975013a4c085baf8c79559cf47/README.md#license
- https://github.com/Parskatt/RoMaV2/blob/ac25bcede24b11975013a4c085baf8c79559cf47/LICENSE
- https://github.com/Parskatt/RoMaV2/issues/49#issuecomment-5636315863
- https://github.com/facebookresearch/dinov3/blob/adc254450203739c8149213a7a69d8d905b4fcfa/LICENSE.md

## Publication

The owner accepted the recorded terms and requested publication on 2026-10-08.
The release workflow verifies the checksummed license bundle, runs source/CPU
checks, collects pinned source snapshots, exports binary notices and builder
sources, then pushes the image with SBOM/provenance and creates its GitHub Release.
The former blanket human-attestation gate is removed. Keep source and notice
assets available for recipients; the image remains subject to upstream terms.
