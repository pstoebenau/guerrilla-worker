"""Resolve or publish a complete, recipe-keyed LichtFeld GitHub Release."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from package_engine_artifact import ARCHIVE, CHECKSUMS, digest, verify


def gh(*arguments):
    return subprocess.run(['gh', *map(str, arguments)], text=True, capture_output=True)


def checked(*arguments):
    result = gh(*arguments)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'GitHub Release command failed')
    return result.stdout


def require_source_assets(records):
    if ('build-source-inventory.json' not in records
            or not any(name.startswith('lichtfeld-source.tar.gz.part') for name in records)
            or not any(name.startswith('lichtfeld-build-dependencies.tar.gz.part') for name in records)):
        raise ValueError('Engine artifact is missing its matching source assets')


def resolve(tag, recipe, destination):
    result = gh('release', 'view', tag, '--json', 'isDraft,assets')
    if result.returncode:
        if 'release not found' in result.stderr.lower() or 'HTTP 404' in result.stderr:
            return {'exists': 'false'}
        raise RuntimeError(result.stderr.strip() or 'Could not resolve engine release')
    release = json.loads(result.stdout)
    if release['isDraft']:
        raise RuntimeError('Engine release is an unfinished draft; finish or remove it before retrying')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    checked('release', 'download', tag, '--dir', destination, '--clobber',
            '--pattern', CHECKSUMS, '--pattern', 'engine-artifact.json')
    records = verify(destination, names=['engine-artifact.json'])
    metadata = json.loads((destination / 'engine-artifact.json').read_text())
    if metadata['recipe'] != recipe:
        raise ValueError('Published engine recipe differs from the requested build')
    require_source_assets(records)
    assets = {asset['name'] for asset in release['assets']}
    if not (set(records) | {CHECKSUMS}) <= assets:
        raise ValueError('Published engine release is missing its binary or matching source assets')
    return {'exists': 'true', 'release_tag': tag, 'sha256': records[ARCHIVE],
            'checksums_sha256': digest(destination / CHECKSUMS)}


def publish(tag, recipe, directory, commit, destination):
    directory = Path(directory)
    records = verify(directory)
    require_source_assets(records)
    metadata = json.loads((directory / 'engine-artifact.json').read_text())
    if metadata['recipe'] != recipe:
        raise ValueError('Packaged engine recipe differs from the requested build')
    notes = (f'LichtFeld Linux amd64 build artifact, recipe {recipe}, worker source {commit}.\n\n'
             'The installed executable, libraries and resources are in lichtfeld-linux-amd64.tar.gz.\n'
             'Verify SHA256SUMS.txt before extraction. Matching patched engine sources and dependency\n'
             'sources are attached; concatenate each set of numbered .part files before extraction.\n')
    with tempfile.TemporaryDirectory(prefix='engine-release-notes-') as temporary:
        path = Path(temporary) / 'notes.md'
        path.write_text(notes, encoding='utf-8')
        result = gh('release', 'create', tag, '--draft', '--target', commit, '--latest=false',
                    '--title', f'LichtFeld Linux build {recipe[:12]}', '--notes-file', path,
                    *(directory / name for name in sorted(records)), directory / CHECKSUMS)
    if result.returncode:
        # Another completed first-time build can win publication. Reuse its
        # verified identity; never overwrite its original files with our build.
        existing = resolve(tag, recipe, destination)
        if existing['exists'] != 'true':
            raise RuntimeError(result.stderr.strip() or 'Could not publish engine artifact')
        return existing
    checked('release', 'edit', tag, '--draft=false', '--latest=false')
    return resolve(tag, recipe, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('resolve', 'publish'))
    parser.add_argument('--recipe', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--commit')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{64}', args.recipe):
        raise ValueError('Engine recipe must be a full SHA-256')
    tag = 'lichtfeld-linux-recipe-' + args.recipe
    if args.command == 'publish':
        if args.directory is None or not re.fullmatch(r'[0-9a-f]{40}', args.commit or ''):
            raise ValueError('Publishing requires an artifact directory and a full source commit')
        output = publish(tag, args.recipe, args.directory, args.commit, args.destination)
    else:
        output = resolve(tag, args.recipe, args.destination)
    if filename := os.environ.get('GITHUB_OUTPUT'):
        with open(filename, 'a', encoding='utf-8') as stream:
            for key, value in output.items():
                stream.write(f'{key}={value}\n')
    print(json.dumps(output))


if __name__ == '__main__':
    main()
