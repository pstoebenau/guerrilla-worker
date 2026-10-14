"""Publish one complete, checksummed release after every native package job succeeds."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from release_assets import ASSET_LIMIT, PLATFORMS, ROOT, record


def collect(artifacts, destination, version, commit):
    manifests = list(artifacts.glob('*/release-*.json'))
    reports = [json.loads(file.read_text()) for file in manifests]
    if len(reports) != len(PLATFORMS) or {report['platform'] for report in reports} != PLATFORMS:
        raise ValueError('Publication requires Windows, Linux, Apple Silicon macOS and Intel macOS')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Combined release directory must be empty')
    for manifest, report in zip(manifests, reports):
        if report['version'] != version or report['commit'] != commit:
            raise ValueError('All release packages must have the same version and commit')
        names = set()
        for expected in report['assets']:
            name = expected['name']
            if Path(name).name != name or name in names:
                raise ValueError('Invalid or duplicate release asset name')
            names.add(name)
            source = manifest.parent / name
            if source.is_symlink() or not source.is_file() or record(source) != expected:
                raise ValueError(f'Release artifact checksum differs: {name}')
        if {file.name for file in manifest.parent.iterdir()} != names | {manifest.name}:
            raise ValueError('Release artifact contains unrecorded files')
        for source in manifest.parent.iterdir():
            target = destination / source.name
            if target.exists():
                raise ValueError(f'Platforms have colliding asset names: {target.name}')
            # Both directories are on the release runner's workspace volume.
            os.link(source, target)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES'):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copy2(ROOT / 'scripts/reassemble_release.py', destination / 'reassemble_release.py')
    source_archive = destination / f'guerrilla-worker-{version}-source.tar.gz'
    subprocess.run(['git', 'archive', '--format=tar.gz', f'--prefix=guerrilla-worker-{version}/',
                    f'--output={source_archive.resolve()}', commit], cwd=ROOT, check=True)
    inventory = [record(file) for file in sorted(destination.iterdir())]
    if any(item['size'] > ASSET_LIMIT for item in inventory):
        raise ValueError('A release asset exceeds GitHub\'s per-file size limit')
    (destination / 'SHA256SUMS.txt').write_text(''.join(f'{item["sha256"]}  {item["name"]}\n' for item in inventory))
    return [record(file) for file in sorted(destination.iterdir())]


def gh(*args, optional=False, input=None):
    result = subprocess.run(['gh', *args], input=input, text=True, capture_output=True)
    if result.returncode:
        if optional and 'HTTP 404' in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def verify_remote(release, assets):
    expected = {asset['name']: asset for asset in assets}
    actual = {asset['name']: asset for asset in release['assets']}
    if set(actual) != set(expected):
        raise ValueError('Uploaded release assets do not match the complete inventory')
    for name, asset in actual.items():
        if asset['state'] != 'uploaded' or asset['size'] != expected[name]['size']:
            raise ValueError(f'Incomplete release upload: {name}')
        if asset.get('digest') and asset['digest'] != 'sha256:' + expected[name]['sha256']:
            raise ValueError(f'Uploaded release checksum differs: {name}')


def find_release(repository, tag):
    # The tag endpoint finds published releases, not drafts. Listing releases
    # includes drafts for the publishing token and gives their stable IDs.
    pages = json.loads(gh('api', f'repos/{repository}/releases', '--paginate', '--slurp'))
    matches = [release for page in pages for release in page if release['tag_name'] == tag]
    if len(matches) > 1:
        raise ValueError(f'Multiple releases have the same tag: {tag}')
    return matches[0] if matches else None


def publish(destination, assets, version, commit, repository):
    tag = f'v{version}'
    release = find_release(repository, tag)
    if release and release['target_commitish'] != commit:
        raise ValueError('Refusing to replace a release belonging to another commit')
    if release and not release['draft']:
        verify_remote(release, assets)
        print(f'Complete release already published: {release["html_url"]}')
        return
    notes = destination.parent / 'release-notes.md'
    notes.write_text(
        f'Guerrilla Worker {version}, built from `{commit}`.\n\n'
        'This release contains Windows x64, Linux x64, macOS Apple Silicon and macOS Intel builds. '
        'All native builds and the Windows installation/uninstallation test passed before publication.\n\n'
        '- Windows: download the setup `.exe` and every matching `.bin` slice into the same folder, then run setup.\n'
        '- Linux: choose AppImage, DEB or RPM. Large packages are split into `.partNNN` files. '
        'Download every part, its `.parts.json`, and `reassemble_release.py`, then run '
        '`python3 reassemble_release.py "PACKAGE.parts.json"`. The script verifies every part and the restored package.\n'
        '- macOS: choose the `aarch64`/`ARM64` package for Apple Silicon or `x64`/`X64` for Intel. '
        'DMG and app archives are included.\n\n'
        'LichtFeld and Spirula remain user-installed. Intel Mac supports worker management; '
        'the current scan engines target NVIDIA Windows/Linux or Apple Silicon. '
        'GPU scan acceptance is separate from these package checks.\n\n'
        'See `SHA256SUMS.txt`, platform runtime/notice archives, and the source archive for checksums and build evidence. '
        'The source archive is the tagged checkout; CI applies the shared release version with '
        f'`python scripts/release_version.py apply --version {version}` before compilation.\n'
    )
    if not release:
        # Use the create response itself: a newly created draft may not yet
        # appear in a subsequent list request from the Actions token.
        release = json.loads(gh('api', f'repos/{repository}/releases', '--method', 'POST', '--input', '-',
            input=json.dumps({'tag_name': tag, 'target_commitish': commit, 'draft': True,
                              'name': f'Guerrilla Worker {version}', 'body': notes.read_text()})))
        if not release or release['target_commitish'] != commit or not release['draft']:
            raise ValueError('The newly created draft release does not match this build')
    gh('release', 'upload', tag, '--repo', repository, '--clobber',
       *(str(destination / asset['name']) for asset in assets))
    release = json.loads(gh('api', f'repos/{repository}/releases/{release["id"]}'))
    verify_remote(release, assets)
    gh('release', 'edit', tag, '--repo', repository, '--draft=false', '--prerelease=false',
       '--latest', '--notes-file', str(notes))
    print(f'Published complete release: https://github.com/{repository}/releases/tag/{tag}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    assets = collect(args.artifacts, args.destination, args.version, os.environ['GITHUB_SHA'])
    publish(args.destination, assets, args.version, os.environ['GITHUB_SHA'], os.environ['GITHUB_REPOSITORY'])
