"""Engine independence and real format compatibility for the pinned CLI."""
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import pipeline_common as common
import platform_runner as platform
from scan_transfer import gaussian_count


CLI = Path(common.__file__).parent / 'docker/converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs'


def scene(path, count=512):
    fields = ['x', 'y', 'z', 'f_dc_0', 'f_dc_1', 'f_dc_2',
              *[f'f_rest_{i}' for i in range(9)], 'opacity',
              'scale_0', 'scale_1', 'scale_2', 'rot_0', 'rot_1', 'rot_2', 'rot_3']
    header = ['ply', 'format binary_little_endian 1.0', f'element vertex {count}',
              *['property float ' + field for field in fields], 'end_header']
    with path.open('wb') as stream:
        stream.write(('\n'.join(header) + '\n').encode())
        for i in range(count):
            values = [i / count, (i % 16) / 16, (i % 32) / 32, 0.1, 0.2, 0.3,
                      *[(i % (j + 2)) / (j + 2) for j in range(9)], 2,
                      -3, -3, -3, 1, 0, 0, 0]
            stream.write(struct.pack('<' + 'f' * len(values), *values))


class ConverterTests(unittest.TestCase):
    def test_spirula_runtime_does_not_probe_lichtfeld(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'spirula'
            executable.write_bytes(b'engine')
            with patch.dict(os.environ, {'SPIRULA_BIN': str(executable)}), \
                    patch.object(common, 'studio_path', side_effect=AssertionError('LichtFeld must not be consulted')), \
                    patch.object(common, 'converter_version', return_value='splat-transform v3.10.1'):
                versions = platform.runtime_versions('spirula')
            self.assertEqual(versions['spirulaBinarySha256'], common.file_hash(executable))
            self.assertEqual(versions['splatTransform'], 'splat-transform v3.10.1')
            self.assertNotIn('lichtfeld', versions)

    @unittest.skipUnless(CLI.is_file() or os.environ.get('SPLAT_TRANSFORM_BIN'), 'Install the locked converter for integration checks')
    def test_real_sog_and_spz_preserve_count_cap_and_sidecar_without_lichtfeld(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'SPLAT_TRANSFORM_GPU': 'cpu'}), \
                patch.object(common, 'studio_path', side_effect=AssertionError('No LichtFeld installation')):
            root = Path(temporary)
            ply, sog, spz = root / 'input.ply', root / 'output.sog', root / 'output.spz'
            scene(ply)
            ply.with_suffix('.ppisp').write_bytes(b'sidecar')
            common.export_sog(ply, sog, root / 'sog.log', 512)
            common.export_spz(ply, spz, root / 'spz.log')
            self.assertEqual(gaussian_count(sog, 512), 512)
            self.assertEqual(platform.spz_count(spz, 512), 512)
            self.assertEqual(sog.with_suffix('.ppisp').read_bytes(), b'sidecar')
            self.assertEqual(spz.with_suffix('.ppisp').read_bytes(), b'sidecar')
            with self.assertRaises(ValueError):
                gaussian_count(sog, 511)
            with self.assertRaises(ValueError):
                platform.spz_count(spz, 511)
            with self.assertRaises(FileExistsError):
                common.export_spz(ply, spz, root / 'overwrite.log')


if __name__ == '__main__':
    unittest.main()
