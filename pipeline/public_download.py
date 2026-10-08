"""Enrolled-only public video fetch with pinned DNS and checked redirects."""
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import urllib.parse
from pathlib import Path

from scan_transfer import google_video_url


def public_addresses(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Expected a public HTTP(S) source URL')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if port not in (80, 443):
        raise ValueError('Source URL port is not allowed')
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Source URL resolves to a non-public address')
    return parsed, port, addresses


def response_for(url):
    for _ in range(6):
        parsed, port, addresses = public_addresses(url)
        family, kind, proto, _, address = addresses[0]
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=60)
        sock = socket.socket(family, kind, proto)
        sock.settimeout(60)
        sock.connect(address)  # Use the checked result, never resolve again.
        if parsed.scheme == 'https':
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        connection.sock = sock
        connection.request('GET', urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, '')),
                           headers={'Host': parsed.hostname, 'User-Agent': 'Guerrilla-Worker/0.1'})
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            target = response.getheader('Location')
            response.close(); connection.close()
            if not target:
                raise ValueError('Source redirect lacks a destination')
            url = urllib.parse.urljoin(url, target)
            continue
        if response.status != 200:
            response.close(); connection.close()
            raise ValueError(f'Source download failed ({response.status})')
        return response, connection
    raise ValueError('Too many source redirects')


def download_public_video(url, destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('Input already exists')
    if urllib.parse.urlsplit(url).hostname in ('photos.app.goo.gl', 'photos.google.com'):
        response, connection = response_for(url)
        try:
            page = response.read(10 * 1024 * 1024 + 1)
            if len(page) > 10 * 1024 * 1024:
                raise ValueError('Source page exceeds size limit')
            url = google_video_url(page.decode('utf-8'))
        finally:
            response.close(); connection.close()
    response, connection = response_for(url)
    temporary = destination.with_suffix('.part')
    try:
        written = 0
        with temporary.open('xb') as stream:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > 5 * 1024 ** 3:
                    raise ValueError('Source exceeds 5 GiB limit')
                stream.write(chunk)
        expected = response.getheader('Content-Length')
        if not written or expected is not None and int(expected) != written:
            raise ValueError('Source download is incomplete')
        report = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type',
                                 '-of', 'json', str(temporary)], check=True, capture_output=True, timeout=60)
        if not any(item.get('codec_type') == 'video' for item in json.loads(report.stdout).get('streams', [])):
            raise ValueError('Source does not contain video')
        temporary.replace(destination)
    finally:
        response.close(); connection.close()
        temporary.unlink(missing_ok=True)
