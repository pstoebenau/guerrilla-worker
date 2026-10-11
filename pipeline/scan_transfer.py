"""Video downloads and Gaussian count verification."""
from __future__ import annotations

from html.parser import HTMLParser
import gzip
import json
import struct
from pathlib import Path
import urllib.parse
import urllib.request
import zipfile


class VideoMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.url = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'meta' and values.get('property') == 'og:video':
            self.url = values.get('content')


def google_video_url(page):
    metadata = VideoMetadata()
    metadata.feed(page)
    url = metadata.url
    parsed = urllib.parse.urlsplit(url or '')
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.googleusercontent.com'):
        raise ValueError('Google Photos did not expose a downloadable video; use a direct video URL or local file')
    # Google Photos og:video is a preview; =dv requests the original download.
    return url.split('=', 1)[0] + '=dv'


def download_video(url, destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {'http', 'https'}:
        raise ValueError('Video URL must use HTTP or HTTPS')
    if parsed.hostname in {'photos.app.goo.gl', 'photos.google.com'}:
        with urllib.request.urlopen(url, timeout=60) as response:
            page = response.read(10 * 1024 * 1024 + 1)
        if len(page) > 10 * 1024 * 1024:
            raise ValueError('Google Photos page is unexpectedly large')
        url = google_video_url(page.decode('utf-8'))
    temporary = destination.with_suffix(destination.suffix + '.part')
    with urllib.request.urlopen(url, timeout=60) as response:
        content_type = response.headers.get_content_type()
        if not (content_type.startswith('video/') or content_type in {
                'application/octet-stream', 'binary/octet-stream'}):
            raise ValueError(f'Expected a video download, received {content_type}')
        expected = response.headers.get('Content-Length')
        written = 0
        with temporary.open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
                written += len(chunk)
        if not written or (expected is not None and written != int(expected)):
            raise ValueError('Video download was empty or incomplete; retry with --resume')
    temporary.replace(destination)


def gaussian_count(path, cap):
    path = Path(path)
    if path.suffix.lower() == '.sog':
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('meta.json')
            if info.file_size > 1024 * 1024:
                raise ValueError('SOG metadata is unexpectedly large')
            metadata = json.loads(archive.read(info))
            count = metadata.get('count')
            if type(count) is not int or not 0 < count <= cap:
                raise ValueError(f'SOG Gaussian count {count} is outside 1..{cap}')
            for field in ('means', 'scales', 'quats', 'sh0'):
                files = metadata[field]['files']
                if not files or any(archive.getinfo(name).file_size == 0 for name in files):
                    raise ValueError(f'SOG {field} textures are missing or empty')
            return count
    with Path(path).open('rb') as stream:
        if stream.readline().strip() != b'ply':
            raise ValueError('Expected a PLY file')
        count = None
        for _ in range(200):
            line = stream.readline(4096).decode('ascii').strip()
            if line.startswith('element vertex '):
                count = int(line.split()[-1])
            if line == 'end_header':
                break
        else:
            raise ValueError('Invalid PLY header')
    if count is None or not 0 < count <= cap:
        raise ValueError(f'PLY Gaussian count {count} is outside 1..{cap}')
    return count


def spz_count(path, cap):
    """Validate the pinned converter's gzip SPZ v1-v3, including CRC and lengths."""
    with gzip.open(path, 'rb') as stream:
        header = stream.read(16)
        if len(header) != 16:
            raise ValueError('SPZ header is truncated')
        magic, version, count, degree, fractional, flags, reserved = struct.unpack('<III4B', header)
        if magic != 0x5053474e or version not in (1, 2, 3) or degree > 3 or reserved or flags & ~1:
            raise ValueError('Unsupported or invalid SPZ header')
        if not 0 < count <= cap or fractional > 24:
            raise ValueError(f'SPZ Gaussian count {count} is outside 1..{cap}')
        stride = (6 if version == 1 else 9) + 3 + (4 if version == 3 else 3) + 1 + 3
        stride += ((degree + 1) ** 2 - 1) * 3
        expected, actual = count * stride, 0
        while block := stream.read(1024 * 1024):
            actual += len(block)
            if actual > expected:
                raise ValueError('SPZ has unexpected trailing attribute data')
        if actual != expected:
            raise ValueError('SPZ attributes are truncated')
    return count
