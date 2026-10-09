"""Collect pinned upstream source snapshots and submodules for release review.

These snapshots are evidence, not an assertion that all corresponding source
has been collected. Build-fetched libraries and binary wheel sources need review
against the actual image inventory. Archives are never extracted or executed.
"""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    'lichtfeld': ('MrNeRF/LichtFeld-Studio', 'd8c50c6a3e2273cb74130a6e9023de8d068af52d', 'LICHTFELD_COMMIT'),
    'spirula': ('harry7557558/spirula-studio', '1943edaf83abf0d00b9ca2d2023424ba1e831d6a', None),
    'densification': ('shadygm/lichtfeld-densification-plugin', 'ab0b04e35b12bff65ee87bdaacfa3177c21521d6', 'DENSIFICATION_PLUGIN_COMMIT'),
    'vcpkg': ('microsoft/vcpkg', '58845ed63eb19aff55e896ea1f5d51f2a0df5b66', 'VCPKG_COMMIT'),
    'dinov3': ('facebookresearch/dinov3', 'adc254450203739c8149213a7a69d8d905b4fcfa', 'DINOV3_COMMIT'),
    'romav2': ('Parskatt/RoMaV2', 'ac25bcede24b11975013a4c085baf8c79559cf47', None),
}


def request(url):
    headers = {'User-Agent': 'guerrilla-worker-release-review'}
    if url.startswith('https://api.github.com/') and os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def api(path):
    with request('https://api.github.com/' + path) as response:
        return json.load(response)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def github_repository(url):
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', url)
    if not match:
        raise ValueError(f'Submodule URL requires explicit review: {url}')
    return match[1]


def collect(repository, commit, destination, records, component, mount='.', ancestors=()):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Source must be pinned to a full commit')
    if (repository, commit) in ancestors:
        raise ValueError('Recursive submodule cycle')
    filename = repository.replace('/', '-') + '-' + commit + '.tar.gz'
    target = destination / filename
    url = f'https://codeload.github.com/{repository}/tar.gz/{commit}'
    previous = next((row for row in records if row['archive'] == filename), None)
    if previous:
        if not target.is_file() or sha256(target) != previous['sha256']:
            raise ValueError(f'Cached source differs from recorded digest: {filename}')
    else:
        temporary = target.with_suffix('.part')
        with request(url) as response, temporary.open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        temporary.replace(target)
    row = {'component': component, 'mount': mount, 'repository': repository,
           'commit': commit, 'url': url, 'archive': filename, 'sha256': sha256(target)}
    if row not in records:
        records.append(row)
    save(destination, records, False)
    tree = api(f'repos/{repository}/git/trees/{commit}?recursive=1')
    if tree.get('truncated'):
        raise ValueError(f'Cannot inventory truncated source tree: {repository}')
    submodules = [item for item in tree['tree'] if item['mode'] == '160000']
    if submodules:
        with request(f'https://raw.githubusercontent.com/{repository}/{commit}/.gitmodules') as response:
            config = configparser.ConfigParser(interpolation=None)
            config.read_string(response.read().decode('utf-8'))
        urls = {section['path']: section['url'] for section in config.values() if 'path' in section}
        for submodule in submodules:
            path = submodule['path']
            if path not in urls:
                raise ValueError(f'Missing submodule URL for {repository}/{path}')
            collect(github_repository(urls[path]), submodule['sha'], destination, records,
                    component, path if mount == '.' else mount + '/' + path,
                    ancestors + ((repository, commit),))
    return row


def save(destination, records, snapshots_complete):
    report = {'version': 1, 'snapshotsComplete': snapshots_complete,
              'correspondingSourceComplete': False,
              'remaining': ['Build-fetched and linked dependencies, including vcpkg source/patches',
                            'Exact source packages for redistributed OS binaries',
                            'Native libraries bundled in Python wheels and engine release archives'],
              'sources': records}
    temporary = destination / 'source-snapshots.json.part'
    temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    temporary.replace(destination / 'source-snapshots.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError('Keep generated upstream archives outside the worker checkout')
    destination.mkdir(parents=True, exist_ok=True)
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8') + '\n' + (ROOT / 'Dockerfile.engine').read_text(encoding='utf-8')
    for _, commit, variable in INPUTS.values():
        if variable and not re.search(r'^ARG ' + variable + '=' + commit + r'$', dockerfile, re.M):
            raise ValueError(f'Update reviewed source pin to match Docker recipes: {variable}')
    if 'ARG SPIRULA_VERSION=2026.9.30\n' not in dockerfile:
        raise ValueError('Update Spirula source pin for the new binary release')
    # Verify tag-to-commit evidence each time; do not silently follow a moved tag.
    for repository, tag, commit in [('harry7557558/spirula-studio', 'v2026.9.30', INPUTS['spirula'][1]),
                                     ('Parskatt/RoMaV2', 'weights', INPUTS['romav2'][1])]:
        reference = api(f'repos/{repository}/git/ref/tags/{tag}')['object']
        if reference != {'sha': commit, 'type': 'commit',
                         'url': f'https://api.github.com/repos/{repository}/git/commits/{commit}'}:
            raise ValueError(f'Review changed upstream tag: {repository}/{tag}')
    index = destination / 'source-snapshots.json'
    records = json.loads(index.read_text(encoding='utf-8'))['sources'] if index.exists() else []
    save(destination, records, False)
    for component, (repository, commit, _) in INPUTS.items():
        print(f'Collecting {component} at {commit}', flush=True)
        collect(repository, commit, destination, records, component)
    save(destination, records, True)
    print(f'Collected {len(records)} source snapshots in {destination}')


if __name__ == '__main__':
    main()
