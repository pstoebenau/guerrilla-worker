"""Stage an explicit, bounded legal-document set for the worker image."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


def package(source, destination, require_license=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    def copy(relative):
        file = source / relative
        if not file.is_file() or file.is_symlink() or any(parent.is_symlink() for parent in file.parents if parent != source):
            raise ValueError(f'Expected a regular reviewed document: {relative}')
        if file.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('Legal document exceeds 10 MiB limit')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)

    for relative in ('THIRD_PARTY_NOTICES', 'README.md', 'SECURITY.md',
                     'docs/dependency-review.md', 'docs/release-process.md',
                     'docs/release-notes-0.1.0.md'):
        copy(relative)
    if (source / 'LICENSE').is_file():
        copy('LICENSE')
    elif require_license:
        raise ValueError('Release image requires the owner-accepted LICENSE')
    else:
        (destination / 'LICENSE-PENDING').write_text('Local candidate only. Original-code license and distribution review are pending.\n')

    manifest_path = source / 'legal/manifest.json'
    if manifest_path.exists():
        copy('legal/manifest.json')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        files = manifest.get('files')
        if manifest.get('version') != 1 or not isinstance(files, list) or not 1 <= len(files) <= 200:
            raise ValueError('Invalid legal-document manifest')
        seen = set()
        for item in files:
            name, digest = item.get('name'), item.get('sha256')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.(txt|md)', name) or name in seen:
                raise ValueError('Invalid or repeated legal-document filename')
            seen.add(name)
            relative = 'legal/' + name
            copy(relative)
            if not isinstance(digest, str) or hashlib.sha256((source / relative).read_bytes()).hexdigest() != digest:
                raise ValueError('Legal-document checksum differs from reviewed manifest')
        if require_license and manifest.get('redistributionReviewed') is not True:
            raise ValueError('Release image requires completed redistribution review')
    elif require_license:
        raise ValueError('Release image requires legal/manifest.json and reviewed third-party notices')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--require-license', action='store_true')
    args = parser.parse_args()
    package(args.source, args.destination, args.require_license)
