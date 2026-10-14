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
    def test_signed_app_survives_image_roundtrip_with_symlinks_and_executable_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder)
            app = bundle / 'macos/Guerrilla Worker.app'
            contents = app / 'Contents'
            binary = contents / 'MacOS/fixture'
            binary.parent.mkdir(parents=True)
            subprocess.run(['clang', '-x', 'c', '-o', str(binary), '-'],
                           input=b'int main(void) { return 0; }\n', check=True)
            sidecar = binary.with_name('sidecar')
            sidecar.write_bytes(binary.read_bytes())
            sidecar.chmod(0o755)
            resources = contents / 'Resources'
            resources.mkdir()
            (resources / 'payload.txt').write_text('sealed resource')
            (resources / 'linked.txt').symlink_to('payload.txt')
            with (contents / 'Info.plist').open('wb') as output:
                plistlib.dump({'CFBundleIdentifier': 'dad.guerrilla.dmg-test',
                              'CFBundleExecutable': 'fixture', 'CFBundlePackageType': 'APPL'}, output)
            subprocess.run(['codesign', '--force', '--sign', '-', str(sidecar)], check=True)
            subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
            image = build_macos_dmg.build(bundle, '0.3.0')
            mount = bundle / 'mounted'
            mount.mkdir()
            try:
                subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(image)],
                               check=True, timeout=30, stdout=subprocess.DEVNULL)
                subprocess.run(['codesign', '--verify', '--deep', '--strict', str(mount / app.name)], check=True)
                # Exercise installation onto the Mac filesystem, as Finder's
                # Applications copy does, without changing any signed files.
                delivered = bundle / 'installed' / app.name
                delivered.parent.mkdir()
                subprocess.run(['ditto', str(mount / app.name), str(delivered)], check=True)
                subprocess.run(['codesign', '--verify', '--deep', '--strict', str(delivered)], check=True)
                subprocess.run([str(delivered / 'Contents/MacOS/fixture')], check=True)
                self.assertTrue((delivered / 'Contents/Resources/linked.txt').is_symlink())
                self.assertEqual((delivered / 'Contents/Resources/linked.txt').read_text(), 'sealed resource')
                self.assertTrue((mount / 'Applications').is_symlink())
                self.assertEqual((mount / 'Applications').readlink(), Path('/Applications'))
            finally:
                if (mount / app.name).exists():
                    subprocess.run(['hdiutil', 'detach', str(mount)], check=True, timeout=30, stdout=subprocess.DEVNULL)

    def test_native_image_tools_build_verify_and_atomically_replace_a_compressed_fixture(self):
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
