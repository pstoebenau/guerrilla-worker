from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_macos_dmg


@unittest.skipUnless(sys.platform == 'darwin', 'Native macOS image tools required')
class MacDmgTests(unittest.TestCase):
    def test_native_image_tools_build_and_verify_a_compressed_fixture_without_attaching(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder)
            executable = bundle / 'macos/Guerrilla Worker.app/Contents/MacOS/fixture'
            executable.parent.mkdir(parents=True)
            executable.write_text('fixture bytes\n')
            executable.chmod(0o755)
            output = build_macos_dmg.build(bundle, '0.3.0')
            self.assertTrue(output.is_file())
            info = plistlib.loads(subprocess.check_output(['hdiutil', 'imageinfo', '-plist', str(output)]))
            self.assertEqual(info['Format'], 'UDZO')
            self.assertEqual(executable.read_text(), 'fixture bytes\n')
            # A normal second build of the same version replaces only the
            # generated image after the new image has passed verification.
            executable.write_text('updated fixture bytes\n')
            self.assertEqual(build_macos_dmg.build(bundle, '0.3.0'), output)
            self.assertEqual(len(list(output.parent.glob('*.dmg'))), 1)


if __name__ == '__main__':
    unittest.main()
