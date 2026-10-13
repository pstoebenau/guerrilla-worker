#!/usr/bin/env python3
"""CPU fixture writing a valid SOG container for count verification."""
import json
from pathlib import Path
import os
import sys
import zipfile

if sys.argv[1:] == ['--version']:
    print('fake-converter-1')
else:
    source, destination = map(Path, sys.argv[-2:])
    count = int(next(line for line in source.read_text().splitlines() if line.startswith('element vertex ')).split()[-1])
    count += int(os.environ.get('FIXTURE_COUNT_DELTA', '0'))
    metadata = {'count': count}
    with zipfile.ZipFile(destination, 'w') as archive:
        for field in ['means', 'scales', 'quats', 'sh0']:
            filename = field + '.webp'
            metadata[field] = {'files': [filename]}
            archive.writestr(filename, b'fixture texture')
        archive.writestr('meta.json', json.dumps(metadata))
