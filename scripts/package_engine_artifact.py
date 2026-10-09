"""Package an installed Linux engine and its matching source, with SHA-256 sums."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import tarfile

from build_source_bundle import SplitArchive


ARCHIVE = 'lichtfeld-linux-amd64.tar.gz'
CHECKSUMS = 'SHA256SUMS.txt'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def normalized(member):
    if any(part in ('.git', '__pycache__') for part in Path(member.name).parts):
        return None
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ''
    return member


def package(engine, destination, recipe, source=None):
    engine, destination = Path(engine), Path(destination)
    if not re.fullmatch(r'[0-9a-f]{64}', recipe):
        raise ValueError('Engine recipe must be a full SHA-256')
    for name in ('bin/run_lichtfeld.sh', 'bin/LichtFeld-Studio'):
        path = engine / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Installed engine is incomplete: {name}')
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / ARCHIVE
    with archive.open('wb') as output, gzip.GzipFile(fileobj=output, filename='', mode='wb', mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode='w|') as stream:
        stream.add(engine, arcname='lichtfeld', filter=normalized)
    if source is not None:
        source = Path(source)
        if not (source / 'CMakeLists.txt').is_file():
            raise ValueError('Matching LichtFeld source is missing')
        with SplitArchive(destination / 'lichtfeld-source.tar.gz') as output, \
                gzip.GzipFile(fileobj=output, filename='', mode='wb', mtime=0) as compressed, \
                tarfile.open(fileobj=compressed, mode='w|') as stream:
            stream.add(source, arcname='lichtfeld-source', filter=normalized)
    metadata = {'version': 1, 'recipe': recipe, 'archive': ARCHIVE,
                'sha256': digest(archive), 'platform': 'linux-amd64', 'cpuBaseline': 'x86-64-v3'}
    (destination / 'engine-artifact.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    files = sorted(path for path in destination.iterdir() if path.is_file() and path.name != CHECKSUMS)
    (destination / CHECKSUMS).write_text(''.join(f'{digest(path)}  {path.name}\n' for path in files), encoding='utf-8')
    return metadata


def verify(directory, checksums_sha256=None, names=None):
    """Verify all assets or an explicit downloaded subset against a pinned manifest."""
    directory = Path(directory)
    manifest = directory / CHECKSUMS
    if checksums_sha256 is not None and digest(manifest) != checksums_sha256:
        raise ValueError('Engine checksum manifest differs from the resolved artifact')
    records = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in records or match[2] in ('.', '..'):
            raise ValueError('Invalid engine checksum manifest')
        records[match[2]] = match[1]
    if ARCHIVE not in records or 'engine-artifact.json' not in records:
        raise ValueError('Engine checksum manifest is incomplete')
    requested = set(records) if names is None else set(names)
    if not requested or not requested <= records.keys():
        raise ValueError('Requested engine assets are missing from the checksum manifest')
    for name in requested:
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != records[name]:
            raise ValueError(f'Engine asset checksum differs: {name}')
    if 'engine-artifact.json' in requested:
        metadata = json.loads((directory / 'engine-artifact.json').read_text())
        if (metadata.get('archive') != ARCHIVE or metadata.get('sha256') != records[ARCHIVE]
                or not re.fullmatch(r'[0-9a-f]{64}', metadata.get('recipe', ''))):
            raise ValueError('Engine identity differs from its checksum manifest')
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pack = commands.add_parser('package')
    pack.add_argument('--engine', type=Path, required=True)
    pack.add_argument('--destination', type=Path, required=True)
    pack.add_argument('--recipe', required=True)
    pack.add_argument('--source', type=Path)
    check = commands.add_parser('verify')
    check.add_argument('directory', type=Path)
    check.add_argument('--checksums-sha256')
    check.add_argument('--asset', action='append')
    args = parser.parse_args()
    if args.command == 'package':
        print(json.dumps(package(args.engine, args.destination, args.recipe, args.source)))
    else:
        verify(args.directory, args.checksums_sha256, args.asset)


if __name__ == '__main__':
    main()
