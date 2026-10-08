"""Preserve dependency source/build material from the actual LichtFeld builder."""
import hashlib
import json
from pathlib import Path
import tarfile


def main():
    destination = Path('/release-evidence')
    destination.mkdir(exist_ok=True)
    roots = [Path('/opt/vcpkg/ports'), Path('/opt/vcpkg/scripts'),
             Path('/opt/vcpkg/triplets'), Path('/opt/vcpkg/downloads'),
             Path('/src/lichtfeld/build/CMakeCache.txt'),
             Path('/src/lichtfeld/build/vcpkg_installed/vcpkg/status')]
    roots.extend(Path('/opt/vcpkg/buildtrees').glob('*/src'))
    roots.extend(Path('/src/lichtfeld/build/_deps').glob('*-src'))
    # Versioned registry ports can differ from the vcpkg checkout's ports.
    roots.extend(Path('/opt/vcpkg/buildtrees/versioning_').glob('versions/*'))
    roots.extend(Path('/src/lichtfeld/build/vcpkg_installed').glob('*/share'))
    roots = sorted({path for path in roots if path.exists()})
    if not any('/buildtrees/' in str(path) and path.name == 'src' for path in roots):
        raise RuntimeError('Builder dependency sources are missing; cannot package release evidence')
    archive = destination / 'lichtfeld-build-dependencies.tar.gz'
    def include(member):
        if '.git' in Path(member.name).parts:
            return None
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        return member
    with tarfile.open(archive, 'w:gz') as stream:
        for root in roots:
            stream.add(root, arcname=root.as_posix().lstrip('/'), filter=include)
    # GitHub release assets have a per-file size limit. Parts are concatenated
    # before extraction; hashes identify both each part and the complete tarball.
    digest = hashlib.sha256()
    parts = []
    with archive.open('rb') as stream:
        index = 0
        while chunk := stream.read(256 * 1024 * 1024):
            part = destination / f'{archive.name}.part{index:03d}'
            part.write_bytes(chunk)
            digest.update(chunk)
            parts.append({'name': part.name, 'sha256': hashlib.sha256(chunk).hexdigest()})
            index += 1
    archive.unlink()
    (destination / 'build-source-inventory.json').write_text(json.dumps({
        'version': 1, 'roots': [str(path) for path in roots],
        'archive': archive.name, 'sha256': digest.hexdigest(), 'parts': parts,
        'restore': 'Concatenate parts in filename order, then extract the resulting tar.gz.',
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()
