"""Discover GPUs using the engine that will actually execute the scan."""
from pathlib import Path
import re
import subprocess
import tempfile
import uuid


def parse_spirula_devices(report):
    devices = re.findall(r'\[([0-9]+)\]\s+([^\n]+?)\s+\((discrete|integrated),\s*([0-9]+) MB\)\s*\n\s*UUID:\s*uuid:([0-9a-fA-F-]+)', report)
    if len(devices) != 1 or devices[0][0] != '0' or int(devices[0][3]) < 1:
        raise RuntimeError('Worker requires exactly one hardware GPU at device 0; CPU renderers are rejected')
    _, name, _, memory, identity = devices[0]
    return [dict(id='gpu-' + identity.lower(), name=name, memoryBytes=int(memory) * 1024 * 1024)]


def probe_spirula(executable):
    absent = Path(tempfile.gettempdir()) / ('guerrilla-preflight-' + uuid.uuid4().hex)
    proc = subprocess.run([str(executable), 'train', '--device', '0', '--data', str(absent),
                           '--cap-max', '1', '--num-iterations', '1', '--disable-viewer', '1', '--keep-viewer-alive', '0'],
                          text=True, capture_output=True, timeout=30)
    report = proc.stdout + proc.stderr
    if 'dataset path does not exist' not in report:
        raise RuntimeError('Spirula GPU initialization failed: ' + report[-1200:])
    return parse_spirula_devices(report)


def nvidia_devices():
    """CUDA-only workers and existing NVIDIA supervisor locks keep their IDs."""
    report = subprocess.check_output(['nvidia-smi', '--query-gpu=uuid,name,memory.total', '--format=csv,noheader,nounits'],
                                     text=True, timeout=30)
    lines = report.strip().splitlines()
    if len(lines) != 1:
        raise RuntimeError('Worker requires one visible NVIDIA GPU; isolate devices before starting')
    identity, name, memory = [part.strip() for part in lines[0].split(',')]
    size = int(memory) * 1024 * 1024
    if not identity or not name or size < 1:
        raise RuntimeError('NVIDIA GPU inventory failed')
    return [dict(id=identity, name=name, memoryBytes=size)]
