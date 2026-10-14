"""Create and verify a compressed DMG while preserving the signed app bundle."""
import argparse
from pathlib import Path
import platform
import re
import subprocess
import tempfile


def build(bundle, version):
    if platform.system() != 'Darwin' or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Build a numeric-version macOS DMG natively on macOS')
    architecture = {'arm64': 'aarch64', 'x86_64': 'x64'}.get(platform.machine())
    if not architecture:
        raise ValueError('Unsupported macOS architecture')
    apps = list((bundle / 'macos').glob('*.app'))
    if len(apps) != 1:
        raise ValueError('Expected exactly one complete Tauri app bundle')
    destination = (bundle / 'dmg').resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f'Guerrilla Worker_{version}_{architecture}.dmg'
    with tempfile.TemporaryDirectory(prefix='guerrilla-dmg-', dir=destination) as folder:
        temporary = Path(folder)
        staging = temporary / 'contents'
        staging.mkdir()
        subprocess.run(['ditto', str(apps[0].resolve()), str(staging / apps[0].name)], check=True)
        (staging / 'Applications').symlink_to('/Applications', target_is_directory=True)
        compressed = temporary / output.name
        # Native folder imaging preserves symlinks, modes and signed resources.
        # makehybrid's HFS writer synthesizes FinderInfo on Mach-O sidecars,
        # invalidating their signatures. Keep Finder/AppleScript out of the
        # image creation flow and bound the native filesystem tool's lifetime.
        subprocess.run(['hdiutil', 'create', '-srcfolder', str(staging), '-fs', 'HFS+',
                        '-volname', 'Guerrilla Worker', '-format', 'UDZO',
                        '-imagekey', 'zlib-level=6', '-o', str(compressed)], check=True, timeout=300)
        subprocess.run(['hdiutil', 'verify', str(compressed)], check=True)
        compressed.replace(output)
    print(f'Native macOS DMG prepared: {output}')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    build(args.bundle, args.version)
