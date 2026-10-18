"""Check format identity in the actual native packages used by the updater."""
from pathlib import Path
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'desktop/src-tauri/target/release/bundle'


def rpm_executable(package):
    verified = subprocess.run(['rpm', '--checksig', '--nosignature', str(package)], capture_output=True)
    if verified.returncode:
        diagnostics = verified.stdout + verified.stderr
        raise AssertionError(f'RPM digest verification failed ({verified.returncode}): {diagnostics.decode(errors="replace")}')
    # rpm-rs 0.16 omits the archive size tag that rpm2cpio requires. rpm2archive
    # supports those packages; verify digests and finish conversion first.
    with tempfile.TemporaryFile() as archive:
        with package.open('rb') as source:
            produced = subprocess.run(['rpm2archive', '--nocompression', '-'], stdin=source,
                                      stdout=archive, stderr=subprocess.PIPE)
        if produced.returncode:
            raise AssertionError(f'rpm2archive failed ({produced.returncode}): {produced.stderr.decode(errors="replace")}')
        archive.seek(0)
        try:
            with tarfile.open(fileobj=archive, mode='r:') as contents:
                matches = [member for member in contents if member.name in (
                    'usr/bin/guerrilla-worker-desktop', './usr/bin/guerrilla-worker-desktop')]
                if len(matches) != 1 or not matches[0].isfile():
                    raise AssertionError('RPM archive must contain one desktop executable')
                executable = contents.extractfile(matches[0]).read()
        except tarfile.TarError as error:
            raise AssertionError(f'Invalid RPM tar archive: {error}') from error
        if not executable:
            raise AssertionError('RPM archive contains an empty desktop executable')
        return executable


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


class RpmExtractionTests(unittest.TestCase):
    def extract_fixture(self, archive=b'', verification=None, conversion=None):
        if verification is None:
            verification = subprocess.CompletedProcess([], 0, b'digests OK', b'')
        if conversion is None:
            conversion = subprocess.CompletedProcess([], 0, None, b'')
        def commands(command, **options):
            if command[0] == 'rpm':
                self.assertEqual(command[1:3], ['--checksig', '--nosignature'])
                return verification
            self.assertEqual(command, ['rpm2archive', '--nocompression', '-'])
            self.assertEqual(options['stdin'].read(), b'RPM fixture')
            options['stdout'].write(archive)
            return conversion
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'fixture.rpm'
            package.write_bytes(b'RPM fixture')
            with patch.object(subprocess, 'run', side_effect=commands) as run:
                try:
                    return rpm_executable(package)
                finally:
                    self.assertEqual(run.call_count, 1 if verification.returncode else 2)

    def test_selected_file_survives_large_trailing_archive_padding(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as contents:
            member = tarfile.TarInfo('./usr/bin/guerrilla-worker-desktop')
            member.size = len(b'executable fixture')
            contents.addfile(member, io.BytesIO(b'executable fixture'))
        self.assertEqual(self.extract_fixture(archive.getvalue() + b'\0' * (4 * 1024 * 1024)),
                         b'executable fixture')

    def test_corrupt_package_fails_digest_verification_before_conversion(self):
        with self.assertRaisesRegex(AssertionError, r'RPM digest verification failed \(1\): fixture.rpm: DIGESTS NOT OK'):
            self.extract_fixture(verification=subprocess.CompletedProcess([], 1, b'fixture.rpm: DIGESTS NOT OK', b''))

    def test_failed_conversion_preserves_diagnostics(self):
        with self.assertRaisesRegex(AssertionError, r'rpm2archive failed \(7\): corrupt compressed payload'):
            self.extract_fixture(b'partial archive', conversion=subprocess.CompletedProcess([], 7, None, b'corrupt compressed payload'))

    def test_invalid_converted_archive_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'Invalid RPM tar archive:'):
            self.extract_fixture(b'not a tar archive')

    def test_missing_executable_is_rejected(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w'):
            pass
        with self.assertRaisesRegex(AssertionError, 'RPM archive must contain one desktop executable'):
            self.extract_fixture(archive.getvalue())
