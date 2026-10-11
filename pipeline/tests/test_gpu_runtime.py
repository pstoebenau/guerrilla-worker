import unittest
from unittest.mock import patch
import subprocess

from gpu_runtime import parse_spirula_devices, probe_spirula


def report(name, kind='discrete', index=0):
    return f'Devices:\n  * [{index}] {name} ({kind}, 8192 MB)\n      UUID: uuid:00001234abcd\n'


class GpuRuntimeTests(unittest.TestCase):
    def test_engine_reports_from_all_supported_gpu_vendors(self):
        for name, kind in [('Apple M3 Max', 'integrated'), ('NVIDIA GeForce RTX 4090', 'discrete'),
                           ('AMD Radeon RX 7900', 'discrete'), ('Intel Arc', 'integrated')]:
            with self.subTest(name=name):
                gpu, = parse_spirula_devices(report(name, kind))
                self.assertEqual(gpu, dict(id='gpu-00001234abcd', name=name, memoryBytes=8 * 1024 ** 3))

    def test_software_renderers_missing_and_ambiguous_devices_are_rejected(self):
        for text in ['', report('llvmpipe', 'cpu'), report('Apple M3 Max', index=1),
                     report('NVIDIA RTX 4090') + report('AMD Radeon', index=1)]:
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, 'hardware GPU'):
                parse_spirula_devices(text)

    def test_gpu_presence_is_not_enough_when_engine_initialization_fails(self):
        with patch('gpu_runtime.subprocess.run', return_value=subprocess.CompletedProcess([], 1, report('Apple M3 Max', 'integrated'), 'Vulkan initialization failed')):
            with self.assertRaisesRegex(RuntimeError, 'initialization failed'):
                probe_spirula('spirula')


if __name__ == '__main__':
    unittest.main()
