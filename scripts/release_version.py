"""Allocate one release version and apply it to every native build checkout."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def numeric_version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError(f'Expected a numeric release version: {value}')
    return tuple(map(int, value.split('.')))


def next_version(base, releases, commit):
    versions, matching = [], []
    for release in releases:
        tag = release['tag_name']
        if not re.fullmatch(r'v\d+\.\d+\.\d+', tag) or release['prerelease']:
            continue
        version = numeric_version(tag[1:])
        versions.append(version)
        if release['target_commitish'] == commit:
            matching.append(version)
    if matching:
        return '.'.join(map(str, max(matching)))
    minimum = numeric_version(base)
    latest = max(versions, default=(0, 0, 0))
    chosen = minimum if minimum > latest else (latest[0], latest[1], latest[2] + 1)
    return '.'.join(map(str, chosen))


def apply_version(root, version):
    numeric_version(version)
    for relative in ('package.json', 'desktop/package.json', 'desktop/src-tauri/tauri.conf.json'):
        file = root / relative
        data = json.loads(file.read_text())
        data['version'] = version
        file.write_text(json.dumps(data, indent=2) + '\n')
    for relative in ('rust/worker/Cargo.toml', 'desktop/src-tauri/Cargo.toml'):
        file = root / relative
        text, count = re.subn(r'(?m)^version = "[^"]+"$', f'version = "{version}"', file.read_text(), count=1)
        if count != 1:
            raise ValueError(f'Missing package version: {relative}')
        file.write_text(text)
    for relative, name in (('Cargo.lock', 'guerrilla-worker'), ('desktop/src-tauri/Cargo.lock', 'guerrilla-worker-desktop')):
        file = root / relative
        pattern = r'(\[\[package\]\]\nname = "' + re.escape(name) + r'"\nversion = ")[^"]+(")'
        text, count = re.subn(pattern, lambda match: match[1] + version + match[2], file.read_text())
        if count != 1:
            raise ValueError(f'Missing locked package version: {name}')
        file.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('select', 'apply'))
    parser.add_argument('--version')
    args = parser.parse_args()
    if args.command == 'apply':
        apply_version(ROOT, args.version)
        return
    releases = json.loads(subprocess.check_output([
        'gh', 'api', f'repos/{os.environ["GITHUB_REPOSITORY"]}/releases', '--paginate', '--slurp'
    ], text=True))
    version = next_version(json.loads((ROOT / 'package.json').read_text())['version'],
                           [release for page in releases for release in page], os.environ['GITHUB_SHA'])
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'version={version}\ntag=v{version}\n')
    print(f'All platforms will build Guerrilla Worker {version}')


if __name__ == '__main__':
    main()
