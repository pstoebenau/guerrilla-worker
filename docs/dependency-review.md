# Dependency and distribution review — 2026-10-07

The owner accepted GPL-3.0-only for the original worker code; the full LICENSE
text and package metadata now record that decision. Third-party components retain their own licenses and terms.
The protocol is an independent data contract and is MIT licensed with owner
approval. Private platform source need not be published merely to
implement this HTTP contract.

## Verified exact upstream sources

| Component and exact input | Evidence | Distribution assessment |
| --- | --- | --- |
| LichtFeld d8c50c6a3e2273cb74130a6e9023de8d068af52d | [LICENSE](https://github.com/MrNeRF/LichtFeld-Studio/blob/d8c50c6a3e2273cb74130a6e9023de8d068af52d/LICENSE) | GPL version 3 text. Binary distribution requires applicable notices and corresponding source, including build scripts and linked dependencies. Inspect per-file terms/submodules before image release. |
| Spirula v2026.9.30; Linux archive SHA256 123d6d0b826388abb64129b6fcf2a8d34fe0662a57ba34e53212a148c891431d | [LICENSE](https://github.com/harry7557558/spirula-studio/blob/v2026.9.30/LICENSE) | GPL version 3 text. Preserve notices and provide corresponding source matching the distributed binary. Release archive needs an inventory of bundled libraries/assets. |
| COLMAP plugin 1afb0b1076da9f60aedc114041cbf21801be745c | [exact tree](https://github.com/shadygm/Lichtfeld-COLMAP-Plugin/tree/1afb0b1076da9f60aedc114041cbf21801be745c), [pyproject](https://github.com/shadygm/Lichtfeld-COLMAP-Plugin/blob/1afb0b1076da9f60aedc114041cbf21801be745c/pyproject.toml) | REMOVED: replaced with Guerrilla reconstruction using the public pycolmap API. No plugin source or binary is distributed. |
| Densification plugin ab0b04e35b12bff65ee87bdaacfa3177c21521d6 | [LICENSE](https://github.com/shadygm/lichtfeld-densification-plugin/blob/ab0b04e35b12bff65ee87bdaacfa3177c21521d6/LICENSE) | GPL version 3 text; retain exact corresponding source and notices. |
| Vendored RoMaV2 at that plugin commit | [LICENSE](https://github.com/shadygm/lichtfeld-densification-plugin/blob/ab0b04e35b12bff65ee87bdaacfa3177c21521d6/RoMaV2/LICENSE) | MIT; retain Johan Edstedt copyright and full license, with the explicit DINOv3 exception. |
| RoMaV2 romav2.pt SHA256 3516ccdbbd8eb89d50dfc0bc4562ccdcc2c60b7908e1819d5aae0cbe1bf979bc | [release](https://github.com/Parskatt/RoMaV2/releases/tag/weights), [author guidance](https://github.com/Parskatt/RoMaV2/issues/49#issuecomment-5636315863) | Owner-accepted assessment: apply repository MIT terms to the author's released model contributions, preserving the separate DINOv3 terms. The author confirms fine-tuning permission; this supports the assessment but is not an express checkpoint redistribution statement. No separate upstream confirmation required. See linux-release-review.md. |
| DINOv3 adc254450203739c8149213a7a69d8d905b4fcfa; archive SHA256 923e23a8cea28c9255fb3c2674ecea3dafc9dcc23259754ab7b91dd02f14d38a | [LICENSE.md](https://github.com/facebookresearch/dinov3/blob/adc254450203739c8149213a7a69d8d905b4fcfa/LICENSE.md) | Custom DINOv3 terms permit limited use/distribution subject to its agreement; require agreement redistribution and restrict reverse engineering and specified uses. Full agreement included in legal/DINOv3-LICENSE.txt. Keeping distinct licenses in one image is not itself relicensing; assess the actual combination and model weights before declaring review complete. |

The custom DINOv3 assessment is based on sections 1(a) and 1(b), not on the
project name. It is not a commercial-use prohibition; it is a distinct restricted
license requiring packaging and compatibility review.

## Remaining exact build inventory

See [Linux release evidence](linux-release-review.md) for source snapshot and
actual-image notice collectors, the remaining source matching work, and the
accepted RoMa checkpoint license assessment. These collectors do not
create a reviewed attestation. Top-level engine and Node license texts are now
also packaged, including Node's third-party notices.

Dockerfile pins CUDA 12.8.1 Ubuntu 24.04 devel/runtime, vcpkg
58845ed63eb19aff55e896ea1f5d51f2a0df5b66, Bun 1.4.2, Node 22.22.0 and
CMake 3.31.6. Base tags are not immutable digests. Apt and transitive Python/vcpkg
dependencies are not fully locked; capture SPDX/CycloneDX SBOM, package licenses,
source revisions and image digests from the final successful build before release.
CUDA runtime redistribution follows NVIDIA terms rather than the worker license;
Ubuntu libraries and FFmpeg carry separate licenses/build-dependent obligations.

Exact top-level Python packages are recorded in pipeline/docker/requirements.txt
and worker-requirements.txt: numpy 2.2.6, OpenCV headless 4.12.0.88,
pycolmap-cuda12 4.0.2, jsonschema 4.26.0, filelock 3.32.7, PyTorch
2.7.1+cu128, torchvision 0.22.1+cu128, Pillow 12.0.0, scipy 1.16.3,
tqdm 4.67.1, einops 0.8.1, rich 14.2.0 and Open3D 0.19.0.
Their installed wheel-level notices and metadata are collected from the actual
image as release assets; NVIDIA terms remain distinct from the worker license.
Native requirements intentionally permit ranges, so an installed environment must
be frozen and audited separately. Bun lock pins TypeScript 5.9.3 and @types/bun
1.3.10 development dependencies. Runtime JS imports only public protocol and Node
standard libraries.

The owner has approved publication under these recorded upstream terms. CI now
packages checksummed notices, upstream source snapshots and dependency sources
from the actual builder. Inventories describe their scope; no blanket legal
certification is asserted. See linux-release-review.md for release contents.

## Actual local image evidence

The independently built worker image
`sha256:49ef94c389f83b6760cf420052f5212ba6ef2f80b659618356c91742890a69c0`
contains 407 dpkg package records and 108 installed Python distribution records.
Version/license metadata and license-file inventories were captured outside the
source checkout as release-review evidence. They are inventories, not completed
legal review. No registry digest exists because the image was not pushed.

Installed JavaScript development-package metadata declares Apache-2.0 for
TypeScript 5.9.3/6.0.3 and MIT for @types/bun 1.3.10, @types/node 26.6.4,
bun-types 1.3.10 and undici-types 8.9.0. The runtime bundle uses Node standard
libraries and the independent public protocol; these development packages are
not runtime imports. Preserve their licenses in any distribution that includes them.

Docker SBOM 0.6.0 / Syft 0.43.0 initially failed because its default Docker API
1.41 is below Engine 29's minimum 1.44. A retry explicitly using API 1.44 was
interrupted during the large image scan to prioritize native GPU acceptance.
No completed SPDX SBOM is claimed. `scripts/inventory.py` captures concrete
dpkg/Python/JavaScript inventories and scanner status, with a bounded SBOM attempt.
