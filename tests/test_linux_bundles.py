"""Check format identity in the actual native packages used by the updater."""
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'desktop/src-tauri/target/release/bundle'


@unittest.skipUnless(sys.platform == 'linux', 'native Linux package integration')
class LinuxBundleTests(unittest.TestCase):
    def test_deb_and_rpm_executables_identify_their_actual_install_format(self):
        debs = list((BUNDLE / 'deb').glob('*.deb'))
        rpms = list((BUNDLE / 'rpm').glob('*.rpm'))
        self.assertEqual(len(debs), 1, 'Build the native packages first')
        self.assertEqual(len(rpms), 1, 'Build the native packages first')
        # Stream the large runtime payload; inspect only the desktop executable.
        process = subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(debs[0])], stdout=subprocess.PIPE)
        deb = None
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                if member.name.lstrip('./') == 'usr/bin/guerrilla-worker-desktop':
                    deb = archive.extractfile(member).read()
                # Drain the stream so dpkg-deb also completes and is checked.
        self.assertEqual(process.wait(), 0)
        self.assertIsNotNone(deb)
        rpm_stream = subprocess.Popen(['rpm2cpio', str(rpms[0])], stdout=subprocess.PIPE)
        extracted = subprocess.run(['cpio', '--extract', '--quiet', '--to-stdout', './usr/bin/guerrilla-worker-desktop'],
                                   stdin=rpm_stream.stdout, capture_output=True)
        rpm_stream.stdout.close()
        self.assertEqual(rpm_stream.wait(), 0)
        self.assertEqual(extracted.returncode, 0, extracted.stderr.decode())
        rpm = extracted.stdout
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
