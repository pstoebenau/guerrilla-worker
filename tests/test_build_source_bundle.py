import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.build_source_bundle import SplitArchive


class SplitArchiveTests(unittest.TestCase):
    def test_split_boundaries_and_hashes(self):
        for size in (1, 7, 8, 9, 24, 25):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / 'sources.tar.gz'
                payload = bytes(range(size))
                with SplitArchive(archive, part_size=8) as output:
                    output.write(b'')
                    output.write(payload[:3])
                    output.write(payload[3:])
                parts = sorted(Path(directory).glob('*.part*'))
                self.assertFalse(archive.exists())
                self.assertEqual(len(parts), (size + 7) // 8)
                self.assertEqual(b''.join(path.read_bytes() for path in parts), payload)
                self.assertEqual(output.digest.hexdigest(), hashlib.sha256(payload).hexdigest())
                for path, item in zip(parts, output.parts):
                    self.assertEqual(item, {
                        'name': path.name,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    })

    def test_streamed_tarball_can_be_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'sources.tar.gz'
            payload = b'dependency source\n' * 100
            with SplitArchive(archive, part_size=32) as output:
                with tarfile.open(fileobj=output, mode='w|gz') as stream:
                    member = tarfile.TarInfo('dependency/source.txt')
                    member.size = len(payload)
                    stream.addfile(member, io.BytesIO(payload))
            compressed = b''.join((Path(directory) / item['name']).read_bytes() for item in output.parts)
            with tarfile.open(fileobj=io.BytesIO(compressed), mode='r:gz') as stream:
                self.assertEqual(stream.extractfile('dependency/source.txt').read(), payload)
            self.assertFalse(archive.exists())


if __name__ == '__main__':
    unittest.main()
