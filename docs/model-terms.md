# Bundled model terms

Guerrilla worker code is GPL-3.0-only. The protocol is MIT. Bundled third-party
code and models retain their own licenses; the worker license does not replace
those terms.

## DINOv3

The complete Meta DINOv3 agreement from pinned revision
`adc254450203739c8149213a7a69d8d905b4fcfa` is included in
`legal/DINOv3-LICENSE.txt` and in the image under
`/usr/share/doc/guerrilla-worker/legal/DINOv3-LICENSE.txt`.

DINOv3 use and redistribution are subject to that agreement. Among other
conditions, it prohibits using or permitting use of the DINO materials for
ITAR-controlled activities or end uses prohibited by trade controls, including
those related to military or warfare purposes, nuclear industries or applications,
espionage, or development or use of guns or illegal weapons. It also includes
reverse-engineering restrictions and requires acknowledgement in published
research results. This summary is not a substitute for the complete agreement.

Redistributors of the DINO materials must pass along the agreement. Including
this notice does not establish the license of every weight file or automatically
resolve compatibility of combined software.

Source: https://github.com/facebookresearch/dinov3/blob/adc254450203739c8149213a7a69d8d905b4fcfa/LICENSE.md

## RoMaV2

The RoMaV2 MIT license is preserved in `legal/RoMaV2-LICENSE.txt`. Our release
assessment applies the repository's MIT grant to the author's code and released
model contributions, with upstream's explicit exception for DINOv3 components.
The exact `romav2.pt` weights are pinned by checksum in Dockerfile. Preserve both
the MIT notice and the DINOv3 agreement; the checkpoint is not described as
entirely MIT. See `docs/linux-release-review.md` for the assessment and evidence.

Source: https://github.com/Parskatt/RoMaV2#license
