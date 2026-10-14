"""Stage native desktop distributions, splitting files below GitHub's asset limit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {'Windows-X64', 'Linux-X64', 'macOS-ARM64', 'macOS-X64'}
ASSET_LIMIT = 2_000_000_000


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def record(file):
    return {'name': file.name, 'size': file.stat().st_size, 'sha256': digest(file)}


def stage_file(file, destination, limit=ASSET_LIMIT):
    original = record(file)
    if original['size'] <= limit:
        target = destination / file.name
        try:
            os.link(file, target)
        except OSError:
            shutil.copy2(file, target)
        return {'file': original, 'assets': [record(target)]}
    parts = []
    with file.open('rb') as source:
        while source.tell() < original['size']:
            target = destination / f'{file.name}.part{len(parts) + 1:03}'
            with target.open('wb') as output:
                remaining = limit
                while remaining and (chunk := source.read(min(8 * 1024 * 1024, remaining))):
                    output.write(chunk)
                    remaining -= len(chunk)
            parts.append(record(target))
    package = {'file': original, 'assets': parts}
    (destination / (file.name + '.parts.json')).write_text(json.dumps(package, indent=2) + '\n')
    return package


def prepare(bundle, destination, platform, version, commit):
    if platform not in PLATFORMS:
        raise ValueError(f'Unsupported release platform: {platform}')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Release staging directory must be empty')
    if platform == 'Windows-X64':
        files = list(bundle.glob('installer/*'))
        setups = [file for file in files if file.name.endswith('-setup.exe')]
        if len(setups) != 1 or not any(file.suffix == '.bin' for file in files):
            raise ValueError('Windows setup and its data slices are required')
        checksum = bundle / 'installer' / (setups[0].stem + '-SHA256SUMS.txt')
        listed = {}
        for line in checksum.read_text().splitlines():
            expected, name = line.split('  ', 1)
            listed[name] = expected
        payloads = [file for file in files if file.suffix in ('.exe', '.bin')]
        if set(listed) != {file.name for file in payloads} or any(digest(file) != listed[file.name] for file in payloads):
            raise ValueError('Windows installer checksums are incomplete or invalid')
    elif platform == 'Linux-X64':
        files = [file for folder, suffix in (('deb', '.deb'), ('rpm', '.rpm'), ('appimage', '.AppImage'))
                 for file in (bundle / folder).glob('*' + suffix)]
        if {file.suffix for file in files} != {'.deb', '.rpm', '.AppImage'}:
            raise ValueError('Linux AppImage, DEB and RPM are all required')
    else:
        files = list((bundle / 'dmg').glob('*.dmg'))
        apps = list((bundle / 'macos').glob('*.app'))
        if len(files) != 1 or len(apps) != 1:
            raise ValueError('The native macOS app and DMG are required')
        # Native tar preserves app symlinks and executable permissions.
        archive = bundle / f'Guerrilla-Worker-{version}-{platform}.app.tar.gz'
        subprocess.run(['tar', '-czf', str(archive.resolve()), '-C', str(apps[0].parent.resolve()), apps[0].name], check=True)
        files.append(archive)
    packages = []
    for file in sorted(files):
        if file.is_file():
            packages.append(stage_file(file, destination))
            # Staged assets now own these generated files. Reclaim the original
            # large package after splitting so all formats fit on the runner.
            file.unlink()
    notices = destination / f'Guerrilla-Worker-{version}-{platform}-notices.tar.gz'
    with tarfile.open(notices, 'w:gz') as archive:
        archive.add(ROOT / 'desktop/src-tauri/resources/runtime/notices', arcname='notices')
        archive.add(ROOT / 'desktop/src-tauri/resources/runtime/manifest.json', arcname='runtime-manifest.json')
    manifest = {'version': version, 'commit': commit, 'platform': platform, 'packages': packages,
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
