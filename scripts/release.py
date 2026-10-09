"""Build a reviewed-source candidate archive; never include runtime state/history."""
import hashlib
import gzip
import json
import re
from pathlib import Path
import sys
import tarfile

root = Path(__file__).resolve().parents[1]
destination = Path(sys.argv[1]).resolve()
destination.mkdir(parents=True, exist_ok=True)
files = [root / name for name in ('package.json', 'bun.lock', 'tsconfig.json', 'Dockerfile', 'Dockerfile.engine', 'Dockerfile.runtime', '.dockerignore', '.gitignore', '.gitattributes', 'README.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES')]
if (root / 'LICENSE').is_file():
    files.append(root / 'LICENSE')
for folder, patterns in {'agent': ['*.ts'], 'tests': ['*.ts', '*.py'], 'packages/protocol': ['package.json', 'README.md', 'LICENSE', 'tsconfig.json', 'src/*.ts', 'scan-settings.schema.json'], 'pipeline': ['*.py', '*.json', 'requirements.txt', 'docker/*requirements.txt', 'docker/converter/package*.json', 'tests/*.py'], 'docs': ['*.md', '*.txt'], 'scripts': ['*.py', '*.mjs', '*.ts', '*.ps1'], 'windows': ['*.cs', '*.csproj', '*.iss', '*.txt'], '.github/workflows': ['*.yml'], 'legal': ['*.md', '*.txt', 'manifest.json'], 'patches': ['*.patch']}.items():
    for pattern in patterns:
        files.extend((root / folder).glob(pattern))
version = json.loads((root / 'package.json').read_text())['version']
if not re.fullmatch(r'\d+\.\d+\.\d+', version):
    raise ValueError('Release version must be numeric major.minor.patch')
archive = destination / f'guerrilla-worker-{version}.tar.gz'
inventory = []
with archive.open('wb') as archive_file, gzip.GzipFile(filename='', mode='wb', fileobj=archive_file, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as stream:
    for file in sorted(set(files)):
        if not file.is_file() or file.is_symlink():
            raise ValueError('Release allowlist contains a missing file or symlink')
        relative = file.relative_to(root).as_posix()
        inventory.append({'path': relative, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
        info = stream.gettarinfo(str(file), arcname=relative)
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mtime = 0
        with file.open('rb') as source:
            stream.addfile(info, source)
(destination / 'worker-release.json').write_text(json.dumps({'archive': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2) + '\n')
(destination / 'release-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
print(json.dumps({'archive': str(archive), 'files': len(inventory), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}))
