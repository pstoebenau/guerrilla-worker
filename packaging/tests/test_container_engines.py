"""Validate installed engine/dependency contracts inside the Linux image.

GPU-free builders verify ELF dependencies, Python ABI, model caches and both
installed engines. Actual LichtFeld imports run when the NVIDIA host driver is
present; only that hardware prerequisite may be absent from a build container.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import unittest


ROOT = Path('/opt/lichtfeld')
PLUGIN = Path('/opt/plugins/densification')
GPU_DRIVER_LIBRARIES = {'libcuda.so.1', 'libnvidia-ml.so.1'}


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def missing_dependencies(path: Path) -> set[str]:
    result = subprocess.run(['ldd', str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(f'ELF dependency inspection failed: {path}\n{result.stdout}\n{result.stderr}')
    return set(re.findall(r'^\s*(\S+)\s+=>\s+not found\s*$', result.stdout, re.M))


@unittest.skipUnless(ROOT.joinpath('artifact-metadata/engine-artifact.json').is_file(), 'Requires the self-contained Linux runtime image')
class ContainerEngineContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.versions = json.loads(Path('/opt/pipeline/runtime-versions.json').read_text())
        sys.path.append(str(ROOT / 'bin'))
        sys.path.append(str(PLUGIN.parent))

    def test_both_engines_are_installed_for_linux_amd64(self):
        self.assertEqual(platform.system(), 'Linux')
        self.assertIn(platform.machine(), {'x86_64', 'amd64'})
        for key in ['LICHTFELD_BIN', 'SPIRULA_BIN']:
            path = Path(os.environ[key])
            self.assertTrue(path.is_file(), f'Missing installed engine: {key}')
            self.assertTrue(os.access(path, os.X_OK), f'Engine is not executable: {key}')
        for path in [ROOT / 'bin/LichtFeld-Studio', Path(os.environ['SPIRULA_BIN'])]:
            with path.open('rb') as stream:
                header = stream.read(20)
            self.assertEqual(header[:4], b'\x7fELF')
            self.assertEqual(int.from_bytes(header[18:20], 'little'), 62, f'Wrong engine ELF architecture: {path}')

    def test_lichtfeld_release_provenance_matches_verified_receipts(self):
        receipts = ROOT / 'artifact-metadata'
        metadata = json.loads((receipts / 'engine-artifact.json').read_text())
        self.assertEqual(metadata['sha256'], self.versions['lichtfeldArtifactSha256'])
        self.assertEqual(metadata['recipe'], self.versions['lichtfeldRecipe'])
        self.assertEqual(metadata['platform'], 'linux-amd64')
        self.assertEqual(metadata['cpuBaseline'], 'x86-64-v3')
        records = {}
        for line in (receipts / 'SHA256SUMS.txt').read_text().splitlines():
            match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_.-]+)', line)
            self.assertIsNotNone(match)
            records[match[2]] = match[1]
        self.assertEqual(records['engine-artifact.json'], digest(receipts / 'engine-artifact.json'))
        self.assertEqual(records['lichtfeld-linux-amd64.tar.gz'], metadata['sha256'])
        self.assertIn('build-source-inventory.json', records)
        self.assertTrue(any(name.startswith('lichtfeld-source.tar.gz.part') for name in records))
        self.assertTrue(any(name.startswith('lichtfeld-build-dependencies.tar.gz.part') for name in records))
        self.assertEqual(digest(receipts / 'SHA256SUMS.txt'), self.versions['lichtfeldChecksumsSha256'])

    def test_native_python_abi_and_gpu_independent_dependencies_resolve(self):
        import torch
        import pycolmap
        self.assertEqual(sys.version_info[:2], (3, 12))
        self.assertEqual(torch.__version__, '2.7.1+cu128')
        self.assertEqual(torch.version.cuda, '12.8')
        self.assertEqual(pycolmap.__version__, '4.0.2')
        self.assertTrue(pycolmap.has_cuda)
        spec = importlib.util.find_spec('lichtfeld')
        self.assertIsNotNone(spec, 'Bundled LichtFeld Python API is undiscoverable')
        extension = Path(spec.origin)
        self.assertTrue(extension.name.startswith('lichtfeld.cpython-312-'))
        for path in [extension, ROOT / 'bin/LichtFeld-Studio']:
            missing = missing_dependencies(path)
            self.assertFalse(missing - GPU_DRIVER_LIBRARIES, f'Missing bundled/system dependencies for {path}: {missing}')

    def test_spirula_command_interface_loads_without_a_scan(self):
        result = subprocess.run([os.environ['SPIRULA_BIN'], 'train', '--help'], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--cap-max', result.stdout + result.stderr)
        self.assertEqual(self.versions['spirulaRelease'], 'v2026.9.30')

    def test_ro_ma_source_and_pinned_models_are_ready_offline(self):
        self.assertTrue((PLUGIN / 'densify.py').is_file())
        self.assertTrue((PLUGIN / 'RoMaV2/src/romav2/__init__.py').is_file())
        commit = (PLUGIN / '.git/HEAD').read_text().strip()
        self.assertEqual(commit, self.versions['lichtfeldDensificationPluginCommit'])
        incomplete = (PLUGIN / 'guerrilla-submodule-status.txt').read_text()
        self.assertFalse(any(line.startswith(('-', '+', 'U')) for line in incomplete.splitlines()))
        sys.path.append(str(PLUGIN / 'RoMaV2/src'))
        import romav2
        self.assertTrue(callable(romav2.RoMaV2))
        hub = Path(os.environ['TORCH_HOME']) / 'hub'
        self.assertEqual(digest(hub / 'checkpoints/romav2.pt'), self.versions['romaWeightsSha256'])
        self.assertTrue((hub / f"facebookresearch_dinov3_{self.versions['dinov3Commit']}" / 'hubconf.py').is_file())
        self.assertTrue((hub / 'trusted_list').is_file())

    def test_actual_lichtfeld_and_plugin_api_imports_with_host_driver(self):
        try:
            import lichtfeld
        except ImportError as error:
            if 'libcuda.so.1' in str(error):
                self.skipTest('NVIDIA host driver is not present in this GPU-free builder')
            raise
        self.assertTrue(Path(lichtfeld.__file__).is_relative_to(ROOT))
        module = importlib.import_module('densification.densify')
        for name in ['build_argparser', 'dense_init', '_camera_record_fingerprint']:
            self.assertTrue(callable(getattr(module, name)), f'Unsupported densification plugin API: {name}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
