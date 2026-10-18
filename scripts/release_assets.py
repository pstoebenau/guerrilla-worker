"""Stage single-file native desktop distributions below GitHub's asset limit."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {'Windows-X64', 'Linux-X64', 'macOS-ARM64'}
ASSET_LIMIT = 2_000_000_000


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def record(file):
    return {'name': file.name, 'size': file.stat().st_size, 'sha256': digest(file)}


def package_name(file, platform, version):
    target = {'Windows-X64': 'windows-x86_64', 'Linux-X64': 'ubuntu-x86_64',
              'macOS-ARM64': 'macos-arm64'}[platform]
    for suffix in ('-setup.exe', '.app.tar.gz', '.AppImage', '.deb', '.rpm', '.dmg'):
        if file.name.endswith(suffix):
            return f'guerrilla-worker-{version}-{target}{suffix}'
    if file.name.endswith('.sig'):
        return package_name(file.with_name(file.name[:-4]), platform, version) + '.sig'
    return file.name  # Private installer evidence retains its original name.


def stage_file(file, destination, limit=ASSET_LIMIT, name=None):
    original = record(file)
    # GitHub normalizes spaces and other special characters in uploaded names.
    # Use that name before making manifests and checksums.
    original['name'] = re.sub(r'[^A-Za-z0-9_.-]', '.', name or original['name'])
    if original['size'] > limit:
        raise ValueError('Desktop package exceeds the single-file release limit; GPU dependencies must be installed during setup')
    target = destination / original['name']
    if target.exists():
        raise ValueError(f'Release asset name collision: {target.name}')
    try:
        os.link(file, target)
    except OSError:
        shutil.copy2(file, target)
    return {'file': original, 'assets': [record(target)]}


def read_signature(file):
    try:
        value = file.read_text().strip()
        decoded = base64.b64decode(value, validate=True).decode('utf-8')
        lines = decoded.splitlines()
        if len(lines) != 4 or not lines[0].startswith('untrusted comment: ') or not lines[2].startswith('trusted comment: '):
            raise ValueError()
        if len(base64.b64decode(lines[1], validate=True)) != 74 or len(base64.b64decode(lines[3], validate=True)) != 64:
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError(f'Missing or malformed Tauri updater signature: {file.name}') from error


def prepare(bundle, destination, platform, version, commit):
    if platform not in PLATFORMS:
        raise ValueError(f'Unsupported release platform: {platform}')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Release staging directory must be empty')
    if platform == 'Windows-X64':
        files = list((bundle / 'nsis').glob('*-setup.exe'))
        if len(files) != 1:
            raise ValueError('Windows requires one self-contained NSIS setup executable')
        updaters = [files[0]]
    elif platform == 'Linux-X64':
        files = [file for folder, suffix in (('deb', '.deb'), ('rpm', '.rpm'), ('appimage', '.AppImage'))
                 for file in (bundle / folder).glob('*' + suffix)]
        if len(files) != 3 or {file.suffix for file in files} != {'.deb', '.rpm', '.AppImage'}:
            raise ValueError('Exactly one Linux AppImage, DEB and RPM are required')
        updaters = files.copy()
    else:
        files = list((bundle / 'dmg').glob('*.dmg'))
        apps = list((bundle / 'macos').glob('*.app'))
        archives = list((bundle / 'macos').glob('*.app.tar.gz'))
        if len(files) != 1 or len(apps) != 1 or len(archives) != 1:
            raise ValueError('The native macOS app, DMG and Tauri updater archive are required')
        # Use the archive produced and signed by Tauri. Repacking would change
        # the signed bytes and invalidate the updater artifact.
        updaters = archives
        files.extend(updaters)
    for updater in updaters:
        signature = updater.with_name(updater.name + '.sig')
        read_signature(signature)
        files.append(signature)
    packages = []
    for file in sorted(files):
        if file.is_file():
            packages.append(stage_file(file, destination, name=package_name(file, platform, version)))
            # Staged assets now own these generated files. Reclaim the original
            # package after staging so all formats fit on the runner.
            file.unlink()
    notices = destination / f'Guerrilla-Worker-{version}-{platform}-notices.tar.gz'
    with tarfile.open(notices, 'w:gz') as archive:
        archive.add(ROOT / 'desktop/src-tauri/resources/runtime/notices', arcname='notices')
        archive.add(ROOT / 'desktop/src-tauri/resources/runtime/manifest.json', arcname='runtime-manifest.json')
    updates = [{'name': package_name(file, platform, version), 'signature': package_name(file, platform, version) + '.sig'} for file in updaters]
    manifest = {'updaters': updates, 'version': version, 'commit': commit, 'platform': platform, 'packages': packages,
                'assets': [record(file) for file in sorted(destination.iterdir())]}
    (destination / f'release-{platform}.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Prepared {platform}: {len(manifest["assets"])} release assets')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    prepare(args.bundle, args.destination, args.platform, args.version, os.environ['GITHUB_SHA'])
