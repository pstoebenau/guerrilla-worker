"""Run beside downloaded .parts.json and .partNNN assets to restore a large package."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def reassemble(manifest):
    package = json.loads(manifest.read_text())
    folder = manifest.parent
    for item in [package['file'], *package['assets']]:
        if Path(item['name']).name != item['name'] or item['name'] in ('', '.', '..'):
            raise ValueError('Package filenames must stay beside the manifest')
    target = folder / package['file']['name']
    if target.exists():
        raise ValueError(f'Refusing to overwrite {target}')
    temporary = target.with_name(target.name + '.assembling')
    try:
        with temporary.open('xb') as output:
            whole = hashlib.sha256()
            for item in package['assets']:
                part_hash, size = hashlib.sha256(), 0
                with (folder / item['name']).open('rb') as source:
                    while chunk := source.read(8 * 1024 * 1024):
                        output.write(chunk)
                        whole.update(chunk)
                        part_hash.update(chunk)
                        size += len(chunk)
                if size != item['size'] or part_hash.hexdigest() != item['sha256']:
                    raise ValueError(f'Invalid part: {item["name"]}')
            if output.tell() != package['file']['size'] or whole.hexdigest() != package['file']['sha256']:
                raise ValueError('Reassembled package checksum differs')
        temporary.rename(target)
        if target.suffix == '.AppImage' and os.name != 'nt':
            target.chmod(target.stat().st_mode | 0o111)
        print(f'Checksum verified: {target}')
        return target
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    reassemble(parser.parse_args().manifest)
