"""Publish a complete, checksummed release after native packages and the Linux image succeed."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote

from release_assets import ASSET_LIMIT, PLATFORMS, ROOT, record, read_signature


def public_packages(report):
    formats = {
        'Windows-X64': ('-setup.exe',),
        'Linux-X64': ('.AppImage', '.deb', '.rpm'),
        'macOS-ARM64': ('.dmg', '.app.tar.gz'),
    }[report['platform']]
    selected = []
    for suffix in formats:
        packages = [package for package in report.get('packages', [])
                    if package['file']['name'].endswith(suffix)]
        if len(packages) != 1:
            raise ValueError(f"Publication requires exactly one {report['platform']} {suffix} package")
        package = packages[0]
        file = package['file']
        if package['assets'] != [file] or file not in report['assets']:
            raise ValueError('Public packages must match the verified single-file inventory')
        selected.append(file)
    return selected


def verify_signature(payload, signature, public_key, version):
    # Delegate cryptographic verification to the upstream Minisign utility.
    # Tauri wraps both Minisign text formats in base64 for configuration/JSON.
    value = read_signature(signature)
    try:
        public = base64.b64decode(public_key, validate=True)
    except ValueError as error:
        raise ValueError('Invalid updater public key') from error
    with tempfile.TemporaryDirectory(prefix='guerrilla-updater-verify-') as directory:
        key = Path(directory) / 'release.pub'
        signed = Path(directory) / 'artifact.minisig'
        key.write_bytes(public)
        signed.write_bytes(base64.b64decode(value, validate=True))
        result = subprocess.run(['minisign', '-V', '-m', str(payload), '-x', str(signed), '-p', str(key)],
                                capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f'Updater signature verification failed: {payload.name}')
    # Minisign verified this comment as part of the authenticated signature.
    comment = base64.b64decode(value).decode('utf-8').splitlines()[2].removeprefix('trusted comment: ')
    versions = [field.removeprefix('version:') for field in comment.split('\t') if field.startswith('version:')]
    if versions != [version]:
        raise ValueError(f'Updater signed version differs from release: {payload.name}')
    return value


def collect(artifacts, destination, version, commit, repository="pstoebenau/guerrilla-worker"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid release repository")
    public_key = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text())["plugins"]["updater"]["pubkey"]
    updates = {}
    manifests = list(artifacts.glob('*/release-*.json'))
    reports = [json.loads(file.read_text()) for file in manifests]
    if len(reports) != len(PLATFORMS) or {report['platform'] for report in reports} != PLATFORMS:
        raise ValueError('Publication requires Windows x86_64, Ubuntu x86_64 and macOS arm64')
    if any(report['version'] != version or report['commit'] != commit for report in reports):
        raise ValueError('All release packages must have the same version and commit')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Combined release directory must be empty')
    for manifest, report in zip(manifests, reports):
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
        # Verify all build evidence above, but publish only installable packages.
        # Manifests, runtime notices and Windows metadata stay in CI artifacts;
        # license/notice texts are also included inside the native packages.
        selected = public_packages(report)
        expected = {
            'Windows-X64': [('-setup.exe', 'windows-x86_64')],
            'Linux-X64': [('.AppImage', 'linux-x86_64-appimage'), ('.deb', 'linux-x86_64-deb'), ('.rpm', 'linux-x86_64-rpm')],
            'macOS-ARM64': [('.app.tar.gz', 'darwin-aarch64')],
        }[report['platform']]
        declared = report.get('updaters', [])
        expected_updates = []
        selected_signatures = []
        for suffix, target in expected:
            payloads = [item for item in selected if item['name'].endswith(suffix)]
            if len(payloads) != 1:
                raise ValueError('Updater manifest must reference the verified native package and its signature')
            updater = {'name': payloads[0]['name'], 'signature': payloads[0]['name'] + '.sig'}
            expected_updates.append(updater)
            signature_name = updater['signature']
            signatures = [item for item in report['assets'] if item['name'] == signature_name]
            if len(signatures) != 1:
                raise ValueError('Updater signature is missing from the verified inventory')
            signature = verify_signature(manifest.parent / updater['name'], manifest.parent / signature_name, public_key, version)
            updates[target] = {
                'signature': signature,
                'url': f"https://github.com/{repository}/releases/download/v{version}/{quote(updater['name'])}",
            }
            selected_signatures.append(signatures[0])
        if sorted(declared, key=lambda item: item['name']) != sorted(expected_updates, key=lambda item: item['name']):
            raise ValueError('Updater manifest must reference the verified native package and its signature')
        for package in [*selected, *selected_signatures]:
            source = manifest.parent / package['name']
            target = destination / source.name
            if target.exists():
                raise ValueError(f'Platforms have colliding asset names: {target.name}')
            # Both directories are on the release runner's workspace volume.
            os.link(source, target)
    source_archive = destination / f'guerrilla-worker-{version}-source.tar.gz'
    subprocess.run(['git', 'archive', '--format=tar.gz', f'--prefix=guerrilla-worker-{version}/',
                    f'--output={source_archive.resolve()}', commit], cwd=ROOT, check=True)
    # The static feed is created only after all three platforms and signatures
    # have passed verification; it remains hidden until the draft is published.
    date = subprocess.check_output(['git', 'show', '-s', '--format=%cI', commit], cwd=ROOT, text=True).strip()
    (destination / 'latest.json').write_text(json.dumps({
        'version': version, 'notes': f'Guerrilla Worker {version}', 'pub_date': date, 'platforms': updates,
    }, indent=2) + '\n')
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


def publish(destination, assets, version, commit, repository, image=None):
    tag = f'v{version}'
    if image and not re.fullmatch(re.escape(f'ghcr.io/{repository.lower()}') + r'@sha256:[a-f0-9]{64}', image):
        raise ValueError('Release image must be an immutable digest in the worker repository')
    release = find_release(repository, tag)
    if release and release['target_commitish'] != commit:
        raise ValueError('Refusing to replace a release belonging to another commit')
    if release and not release['draft']:
        verify_remote(release, assets)
        print(f'Complete release already published: {release["html_url"]}')
        return
    notes = destination.parent / 'release-notes.md'
    docker_note = (f'- Linux NVIDIA Docker: `docker pull ghcr.io/{repository.lower()}:{tag}`. '
                   'The matching image includes Spirula Vulkan, its dense reconstruction model and converter runtime. '
                   f'Immutable image: `{image}`.\n' if image else '')
    notes.write_text(
        f'Guerrilla Worker {version}, built from `{commit}`.\n\n'
        'This release contains Windows x86_64, Ubuntu x86_64 and macOS arm64 builds. '
        'All native builds and the Windows installation/uninstallation test passed before publication.\n\n'
        '- Windows: download and run the single setup `.exe`.\n'
        '- Linux: download AppImage, DEB or RPM; signed in-app updates preserve the installed format. DEB/RPM installation may request administrator authorization.\n'
        f'{docker_note}'
        '- macOS: choose the `arm64` package for Apple Silicon. '
        'Copy the app from its DMG to Applications before opening it; app archives are also included. '
        'Builds use ad-hoc signing; after the first launch attempt, '
        'macOS may require System Settings > Privacy & Security > Open Anyway. Apple Developer ID notarization is not configured.\n\n'
        'Spirula and its reconstruction model are installed and checksum-verified automatically on Windows, Linux and Apple Silicon macOS. '
        'Use Setup > Install / repair runtime to prepare them in advance. Initial setup needs internet and about 1.2 GB of downloads. '
        'The app bundles its Node/FFmpeg converter runtime. '
        'GPU scan acceptance is separate from these package checks.\n\n'
        'Windows, macOS and Linux builds support signed in-app updates. Updater signing does not provide Windows Authenticode or Apple notarization. '
        'See `SHA256SUMS.txt` for download checksums. Licenses and third-party notices are included inside the packages '
        'and source archive; platform manifests and additional build evidence remain in the workflow artifacts. '
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
    latest = gh('api', f'repos/{repository}/releases/latest', optional=True)
    latest_tag = json.loads(latest)['tag_name'] if latest else None
    newer = latest_tag and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', latest_tag) and tuple(map(int, latest_tag[1:].split('.'))) > tuple(map(int, version.split('.')))
    gh('release', 'edit', tag, '--repo', repository, '--draft=false', '--prerelease=false',
       '--latest=false' if newer else '--latest', '--notes-file', str(notes))
    print(f'Published complete release: https://github.com/{repository}/releases/tag/{tag}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--image')
    args = parser.parse_args()
    assets = collect(args.artifacts, args.destination, args.version, os.environ['GITHUB_SHA'], os.environ['GITHUB_REPOSITORY'])
    publish(args.destination, assets, args.version, os.environ['GITHUB_SHA'], os.environ['GITHUB_REPOSITORY'], args.image)
