"""Reuse a verified registry image even before its GitHub Release exists."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from release_version import numeric_version


class ImageConflict(ValueError):
    pass


def select_version(repository, version, commit):
    # Failed releases may already own a versioned image. Never overwrite another
    # commit's image when the next push allocates its release version.
    for _ in range(100):
        try:
            existing_image(repository, version, commit)
            return version
        except ImageConflict:
            major, minor, patch = numeric_version(version)
            version = f'{major}.{minor}.{patch + 1}'
    raise ValueError('Too many occupied image versions; choose a new source version')


def inspect(reference, template, optional=False):
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', reference,
                             '--format', template], capture_output=True, text=True)
    if result.returncode:
        # Only an explicitly missing manifest permits a build. Authentication,
        # rate limits and network failures must not masquerade as cache misses.
        error = result.stderr.lower()
        if optional and ('manifest unknown' in error or f'{reference.lower()}: not found' in error):
            return None
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def existing_image(repository, version, commit):
    numeric_version(version)
    image = f'ghcr.io/{repository.lower()}'
    digest = inspect(f'{image}:v{version}', '{{.Manifest.Digest}}', optional=True)
    if digest is None:
        return None
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Invalid registry image digest')
    # Resolve the tag once, then verify and reuse that immutable digest.
    config = json.loads(inspect(f'{image}@{digest}', '{{json .Image}}'))
    config = config.get('linux/amd64', config)
    labels = config.get('config', {}).get('Labels', {})
    expected = {'org.opencontainers.image.version': version,
                'org.opencontainers.image.revision': commit,
                'org.opencontainers.image.source': f'https://github.com/{repository}'}
    if (config.get('os') != 'linux' or config.get('architecture') != 'amd64'
            or any(labels.get(key) != value for key, value in expected.items())):
        raise ImageConflict('Existing image does not match this version, commit, repository or platform; '
                         'refusing reuse or overwrite. Select a new release version for changed source.')
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--metadata-file', required=True, type=Path)
    args = parser.parse_args()
    digest = existing_image(os.environ['GITHUB_REPOSITORY'], args.version, os.environ['GITHUB_SHA'])
    if digest:
        args.metadata_file.write_text(json.dumps({'containerimage.digest': digest}) + '\n')
        print(f'Reusing verified Linux image {digest}')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'image_exists={str(digest is not None).lower()}\n')


if __name__ == '__main__':
    main()
