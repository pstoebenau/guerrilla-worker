from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scan_transfer import download_video, gaussian_count, google_video_url


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_google_photos_original_and_non_video_page(self):
        page = '<meta content="https://lh3.googleusercontent.com/pw/video=w600-h315-k-no-m18" property="og:video">'
        self.assertEqual(google_video_url(page), 'https://lh3.googleusercontent.com/pw/video=dv')
        for page in ['<html>Sign in</html>', '<meta property="og:video" content="https://example.com/video">']:
            with self.assertRaises(ValueError):
                google_video_url(page)

    def test_incomplete_or_html_download_never_becomes_source(self):
        for content_type, length in [('video/mp4', '100'), ('text/html', '3')]:
            response = io.BytesIO(b'123')
            response.headers = Message()
            response.headers['Content-Type'] = content_type
            response.headers['Content-Length'] = length
            with patch('scan_transfer.urllib.request.urlopen', return_value=response):
                with self.assertRaises(ValueError):
                    download_video('https://example.com/video', self.root/'source.mp4')
            self.assertFalse((self.root/'source.mp4').exists())

    def test_sog_count_and_cap(self):
        path = self.root / 'scene.sog'
        with zipfile.ZipFile(path, 'w') as archive:
            metadata = {'count': 3}
            for field in ('means', 'scales', 'quats', 'sh0'):
                metadata[field] = {'files': [field + '.webp']}
                archive.writestr(field + '.webp', b'texture')
            archive.writestr('meta.json', json.dumps(metadata))
        self.assertEqual(gaussian_count(path, 3), 3)
        with self.assertRaises(ValueError):
            gaussian_count(path, 2)

    def test_sog_missing_textures_and_invalid_count_rejected(self):
        sog = self.root / 'bad.sog'
        for count in (True, 0, 3000001, 3):
            with zipfile.ZipFile(sog, 'w') as archive:
                archive.writestr('meta.json', json.dumps({'count': count}))
            with self.assertRaises((ValueError, KeyError)):
                gaussian_count(sog, 3000000)


if __name__ == '__main__':
    unittest.main()
