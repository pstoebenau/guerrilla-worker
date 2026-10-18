"""Emit a tar.gz of actual installed license evidence to stdout inside an image.

No image files are changed. Missing notices remain explicit review findings.
Run through inventory.py, which saves this stream outside the checkout.
"""
import gzip
import hashlib
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
    records, problems = {}, []
    prefix = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/')
    with gzip.GzipFile(fileobj=sys.stdout.buffer, mode='wb', mtime=0) as zipped, \
            tarfile.open(fileobj=zipped, mode='w|') as archive:
        for relative in ['usr/share/doc', 'opt/spirula', 'opt/converter/node_modules']:
            root = prefix / relative
            if not root.exists():
                problems.append({'path': str(root), 'reason': 'Notice root absent'})
                continue
            for path in sorted(root.rglob('*')):
                if notice_name(path) and not path.is_dir():
                    add_document(archive, path, root, records, problems)
        report = json.dumps({'version': 1, 'redistributionReviewed': False,
                             'files': records,
                             'findings': problems}, indent=2).encode()
        info = tarfile.TarInfo('notice-inventory.json')
        info.size = len(report)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(report))


if __name__ == '__main__':
    main()
