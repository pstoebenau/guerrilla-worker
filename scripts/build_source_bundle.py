"""Preserve dependency source/build material from the actual LichtFeld builder."""
import hashlib
import json
from pathlib import Path
import tarfile


class SplitArchive:
    """Write compressed bytes directly to parts, without a second full copy."""

    def __init__(self, archive, part_size=256 * 1024 * 1024):
        if part_size <= 0:
            raise ValueError('part_size must be positive')
        self.archive = archive
        self.part_size = part_size
        self.digest = hashlib.sha256()
        self.parts = []
        self.stream = None
        self.size = 0

    def write(self, data):
        length = len(data)
        data = memoryview(data)
        while data:
            if self.stream is None:
                self.path = self.archive.with_name(f'{self.archive.name}.part{len(self.parts):03d}')
                self.stream = self.path.open('wb')
                self.part_digest = hashlib.sha256()
                self.size = 0
            chunk = data[:self.part_size - self.size]
            self.stream.write(chunk)
            self.digest.update(chunk)
            self.part_digest.update(chunk)
            self.size += len(chunk)
            data = data[len(chunk):]
            if self.size == self.part_size:
                self.close_part()
        return length

    def close_part(self):
        if self.stream is not None:
            self.stream.close()
            self.parts.append({'name': self.path.name, 'sha256': self.part_digest.hexdigest()})
            self.stream = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close_part()


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
    if not Path('/opt/vcpkg/downloads').is_dir() or not any(Path('/opt/vcpkg/downloads').iterdir()):
        raise RuntimeError('Dependency source downloads are missing; cannot package release evidence')
    archive = destination / 'lichtfeld-build-dependencies.tar.gz'
    def include(member):
        if '.git' in Path(member.name).parts:
            return None
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        return member
    # GitHub release assets have a per-file size limit. Stream compressed data
    # directly into parts so packaging never retains the complete tarball too.
    with SplitArchive(archive) as output, tarfile.open(fileobj=output, mode='w|gz') as stream:
        for root in roots:
            stream.add(root, arcname=root.as_posix().lstrip('/'), filter=include)
    (destination / 'build-source-inventory.json').write_text(json.dumps({
        'version': 1, 'roots': [str(path) for path in roots],
        'archive': archive.name, 'sha256': output.digest.hexdigest(), 'parts': output.parts,
        'restore': 'Concatenate parts in filename order, then extract the resulting tar.gz.',
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()
