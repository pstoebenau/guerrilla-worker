#!/usr/bin/env python3
"""CPU fixture implementing the real Spirula command/artefact contract."""
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tarfile
import time

args = sys.argv[1:]

def option(name):
    return args[args.index(name) + 1]

if args == ['--version']:
    print('fake-engine-1')
elif args[:2] == ['sam', 'extract']:
    output = Path(option('--out'))
    output.mkdir(parents=True)
    if os.environ.get('FIXTURE_FAIL_SELECTION') == '1':
        (output / 'partial.jpg').write_bytes(b'partial')
        sys.exit(1)
    for index in range(3):
        (output / f'{index}.jpg').write_bytes(b'fixture image')
elif args[:2] == ['sfm', 'auto']:
    output = Path(option('-o')) / 'sparse' / '0'
    output.mkdir(parents=True)
    for name in ['cameras.bin', 'images.bin', 'points3D.bin']:
        (output / name).write_bytes(b'fixture colmap')
elif args[:1] == ['train']:
    if not Path(option('--data')).exists():
        print('[0] Fixture GPU (integrated, 8192 MB)\n  UUID: uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        print('dataset path does not exist')
        sys.exit(1)
    count = int(option('--cap-max'))
    if '--resume' in args and not Path(option('--resume')).joinpath('state.tar').is_file():
        raise RuntimeError('Native resume checkpoint unavailable')
    run = Path(option('--output-dir-prefix')) / option('--output-dir-name')
    run.mkdir(parents=True, exist_ok=True)
    (run / 'config.json').write_text(json.dumps({'fixture': True}))
    if '--resume' in args:
        (run / 'resumed-from').write_text(option('--resume'))
    for step in [1, 2]:
        directory = run / f'step-{step}.ckpt'
        directory.mkdir(exist_ok=True)
        metadata = json.dumps({'full_resume': 1, 'cur_num_splats': count}).encode()
        with tarfile.open(directory / 'state.tar', 'w') as archive:
            for name, data in [('state.json', metadata), ('world.means.npy', b'npy-fixture')]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        (directory / 'splat.ply').write_text(f'ply\nformat ascii 1.0\nelement vertex {count}\nproperty float x\nend_header\n' + ('0\n' * count))
    print('iteration 2/2')
    if os.environ.get('FIXTURE_HOLD') == '1':
        descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        (run / 'engine.pid').write_text(str(os.getpid()))
        (run / 'descendant.pid').write_text(str(descendant.pid))
        time.sleep(60)
else:
    raise RuntimeError(f'Unsupported fixture invocation: {args}')
