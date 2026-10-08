import socket
import unittest
from unittest.mock import patch
from public_download import public_addresses


class PublicSourceTests(unittest.TestCase):
    def test_rejects_private_ipv4_ipv6_and_mixed_dns(self):
        for addresses in [('127.0.0.1',), ('169.254.169.254',), ('::1',), ('10.0.0.1',), ('8.8.8.8', '192.168.1.2')]:
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443)) for address in addresses]
            with patch('socket.getaddrinfo', return_value=records):
                with self.assertRaises(ValueError):
                    public_addresses('https://source.example/video')

    def test_rejects_credentials_and_unapproved_ports(self):
        for url in ('file:///tmp/video', 'https://user:password@example.com/video', 'https://example.com:8080/video'):
            with self.assertRaises(ValueError):
                public_addresses(url)

    def test_returns_exact_checked_address_for_pinned_connection(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        with patch('socket.getaddrinfo', return_value=records):
            self.assertEqual(public_addresses('https://source.example/video')[2], records)
