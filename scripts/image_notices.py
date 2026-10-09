"""Emit a tar.gz of actual installed license evidence to stdout inside an image.

No image files are changed. Missing notices remain explicit review findings.
Run through inventory.py, which saves this stream outside the checkout.
"""
import gzip
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import re
import sys
import tarfile


NOTICE = re.compile(r'^(?:licen[cs]e|copying|copyright|notice|authors|eula|third.party)(?:$|[._-])', re.I)
LIMIT = 10 * 1024 * 1024


def notice_name(path):
    return bool(NOTICE.match(path.name))


def add_document(archive, path, root, records, problems):
    # Debian copyright links commonly point to another package's copyright file.
    # Preserve their bytes under the original name, bounded to the allowed root.
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        problems.append({'path': str(path), 'reason': 'Missing file or link outside notice root'})
        return False
    if resolved.stat().st_size > LIMIT:
        problems.append({'path': str(path), 'reason': 'Notice exceeds 10 MiB'})
        return False
    name = 'files/' + path.as_posix().lstrip('/')
    if name in records:
        return True
    data = resolved.read_bytes()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(data))
    records[name] = {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}
    return True


def main():
    records, problems, packages = {}, [], []
    with gzip.GzipFile(fileobj=sys.stdout.buffer, mode='wb', mtime=0) as zipped, \
            tarfile.open(fileobj=zipped, mode='w|') as archive:
        for root in map(Path, ['/usr/share/doc', '/opt/lichtfeld', '/opt/spirula',
                               '/opt/plugins', '/opt/converter/node_modules', '/usr/local/cuda']):
            if not root.exists():
                problems.append({'path': str(root), 'reason': 'Notice root absent'})
                continue
            for path in sorted(root.rglob('*')):
                if notice_name(path) and not path.is_dir():
                    add_document(archive, path, root, records, problems)
        for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
            package = {'name': dist.metadata['Name'], 'version': dist.version,
                       'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'),
                       'notices': []}
            root = Path(dist.locate_file('')).resolve()
            for relative in dist.files or []:
                path = Path(dist.locate_file(relative))
                if notice_name(path) or any(part.lower() in ('licenses', 'licences') for part in relative.parts):
                    if add_document(archive, path, root, records, problems):
                        package['notices'].append(str(relative))
                # Metadata retains upstream URLs, license classifiers and notices
                # even for old wheels without a separate license file.
                elif path.name == 'METADATA':
                    add_document(archive, path, root, records, problems)
            if not package['notices']:
                problems.append({'package': package['name'], 'reason': 'No separate wheel notice found; inspect metadata/upstream'})
            packages.append(package)
        report = json.dumps({'version': 1, 'redistributionReviewed': False,
                             'files': records, 'pythonPackages': packages,
                             'findings': problems}, indent=2).encode()
        info = tarfile.TarInfo('notice-inventory.json')
        info.size = len(report)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(report))


if __name__ == '__main__':
    main()
