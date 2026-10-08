"""Capture actual built-image package evidence outside the source checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys

image, output = sys.argv[1], Path(sys.argv[2]).resolve()
root = Path(__file__).resolve().parents[1]
if output == root or root in output.parents:
    raise ValueError('Generated inventory must be outside the source checkout')
output.mkdir(parents=True, exist_ok=True)


def capture(name, command):
    result = subprocess.run(command, check=True, capture_output=True)
    (output / name).write_bytes(result.stdout)


inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], timeout=30))[0]
(output / 'image-summary.json').write_text(json.dumps({key: inspected.get(key) for key in ('Id', 'RepoDigests', 'Architecture', 'Os', 'Size', 'Created')}, indent=2) + '\n')
licenses = json.loads(subprocess.check_output(['bun', 'pm', 'licenses', '--json'], cwd=root))
for group in licenses.values():
    for package in group:
        package.pop('paths', None)
(output / 'javascript-licenses.json').write_text(json.dumps(licenses, indent=2) + '\n')
capture('python-packages.json', ['docker', 'run', '--rm', '--entrypoint', 'python', image, '-c',
    "import importlib.metadata as m,json; print(json.dumps([{'name':d.metadata['Name'],'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License',''),'licenseFiles':[str(f) for f in d.files or [] if 'license' in str(f).lower() or 'copying' in str(f).lower()]} for d in m.distributions()],indent=2))"])
capture('os-packages.json', ['docker', 'run', '--rm', '--entrypoint', 'python', image, '-c',
    "import json,subprocess; rows=subprocess.check_output(['dpkg-query','-W','-f=${Package}\\t${Version}\\t${Architecture}\\t${source:Package}\\t${source:Version}\\n'],text=True); print(json.dumps([dict(zip(['name','version','architecture','sourcePackage','sourceVersion'],line.split(chr(9)))) for line in rows.splitlines()],indent=2))"])
with (output / 'binary-notices.tar.gz.part').open('wb') as archive:
    subprocess.run(['docker', 'run', '--rm', '--network=none', '-i', '--entrypoint', 'python', image, '-'],
                   input=(root / 'scripts/image_notices.py').read_bytes(), stdout=archive,
                   check=True, timeout=300)
(output / 'binary-notices.tar.gz.part').replace(output / 'binary-notices.tar.gz')
environment = dict(os.environ, DOCKER_API_VERSION=os.environ.get('DOCKER_API_VERSION', '1.44'))
version = subprocess.run(['docker', 'sbom', 'version'], capture_output=True, text=True).stdout
try:
    scan = subprocess.run(['docker', 'sbom', '--format', 'spdx-json', '--output', str(output / 'image.spdx.json'), image], capture_output=True, env=environment, timeout=900)
    status = {'exitCode': scan.returncode, 'stderr': scan.stderr.decode(errors='replace')}
except subprocess.TimeoutExpired:
    status = {'exitCode': None, 'stderr': 'Scanner exceeded 900 seconds; retain package inventories as incomplete fallback.'}
status.update(scannerVersion=version, legalReviewComplete=False)
(output / 'scanner-status.json').write_text(json.dumps(status, indent=2) + '\n')
print(json.dumps({'output': str(output), 'sbomComplete': status['exitCode'] == 0, 'legalReviewComplete': False}))
