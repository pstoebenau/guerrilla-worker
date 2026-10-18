"""Check format identity in the actual native packages used by the updater."""
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'desktop/src-tauri/target/release/bundle'


def rpm_executable(package):
    # cpio may stop at its archive trailer before rpm2cpio has written all
    # trailing bytes. Finish and verify decompression before selecting a file.
    with tempfile.TemporaryFile() as archive:
        produced = subprocess.run(['rpm2cpio', str(package)], stdout=archive, stderr=subprocess.PIPE)
        if produced.returncode:
            raise AssertionError(f'rpm2cpio failed ({produced.returncode}): {produced.stderr.decode(errors="replace")}')
        archive.seek(0)
        extracted = subprocess.run(['cpio', '--extract', '--quiet', '--to-stdout',
                                    'usr/bin/guerrilla-worker-desktop', './usr/bin/guerrilla-worker-desktop'],
                                   stdin=archive, capture_output=True)
        if extracted.returncode:
            raise AssertionError(f'cpio failed ({extracted.returncode}): {extracted.stderr.decode(errors="replace")}')
        if not extracted.stdout:
            raise AssertionError('cpio archive does not contain the desktop executable')
        return extracted.stdout


@unittest.skipUnless(sys.platform == 'linux', 'native Linux package integration')
class LinuxBundleTests(unittest.TestCase):
    def test_deb_and_rpm_executables_identify_their_actual_install_format(self):
        debs = list((BUNDLE / 'deb').glob('*.deb'))
        rpms = list((BUNDLE / 'rpm').glob('*.rpm'))
        self.assertEqual(len(debs), 1, 'Build the native packages first')
        self.assertEqual(len(rpms), 1, 'Build the native packages first')
        # Stream the large runtime payload; inspect only the desktop executable.
        deb = None
        with subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(debs[0])], stdout=subprocess.PIPE) as process:
            with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
                for member in archive:
                    if member.name.lstrip('./') == 'usr/bin/guerrilla-worker-desktop':
                        deb = archive.extractfile(member).read()
            # tarfile stops at the tar terminator, which can precede padding.
            while process.stdout.read(1024 * 1024):
                pass
            self.assertEqual(process.wait(), 0)
        self.assertIsNotNone(deb)
        rpm = rpm_executable(rpms[0])
        # Tauri embeds comparator literals as well as the live marker. Checking
        # only for a substring would pass when an RPM accidentally wraps a DEB.
        marker = b'__TAURI_BUNDLE_TYPE_VAR_DEB'
        replacement = b'__TAURI_BUNDLE_TYPE_VAR_RPM'
        candidates = []
        start = 0
        while (offset := deb.find(marker, start)) >= 0:
            if deb[:offset] + replacement + deb[offset + len(marker):] == rpm:
                candidates.append(offset)
            start = offset + len(marker)
        self.assertEqual(len(candidates), 1, 'DEB/RPM must differ only at the live Tauri bundle marker')


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('cpio'), 'GNU cpio extraction regression')
class RpmExtractionTests(unittest.TestCase):
    def extract_fixture(self, producer):
        run = subprocess.run
        def commands(command, **options):
            if command[0] == 'rpm2cpio':
                command = [sys.executable, '-c', producer]
            return run(command, **options)
        with patch.object(subprocess, 'run', side_effect=commands):
            return rpm_executable(Path('fixture.rpm'))

    def test_selected_file_survives_large_trailing_archive_padding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = 'usr/bin/guerrilla-worker-desktop'
            file = root / relative
            file.parent.mkdir(parents=True)
            file.write_bytes(b'executable fixture')
            archive = subprocess.run(['cpio', '--create', '--format=newc', '--quiet'],
                                     cwd=root, input=('./' + relative + '\n').encode(), capture_output=True, check=True).stdout
            padded = root / 'padded.cpio'
            padded.write_bytes(archive + b'\0' * (4 * 1024 * 1024))
            producer = f'import shutil, sys; shutil.copyfileobj(open({str(padded)!r}, "rb"), sys.stdout.buffer)'
            self.assertEqual(self.extract_fixture(producer), b'executable fixture')

    def test_failed_decompression_preserves_producer_diagnostics(self):
        with self.assertRaisesRegex(AssertionError, r'rpm2cpio failed \(7\): corrupt compressed payload'):
            self.extract_fixture('import sys; sys.stdout.buffer.write(b"partial archive"); sys.stderr.write("corrupt compressed payload"); sys.exit(7)')

    def test_failed_extraction_preserves_consumer_diagnostics(self):
        with self.assertRaisesRegex(AssertionError, r'cpio failed \([1-9][0-9]*\): .+'):
            self.extract_fixture('import sys; sys.stdout.buffer.write(b"not a cpio archive")')
