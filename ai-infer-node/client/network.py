"""Plain HTTP is allowed only for explicit local, LAN or Tailnet addresses."""
import ipaddress
from urllib.parse import urlsplit

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    '100.64.0.0/10', '::1/128', 'fc00::/7',
))


def private_host(host):
    if host == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host or '')
        return any(address in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def validate_url(value):
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username
            or url.password or url.query or url.fragment or '\\' in value
            or any(char.isspace() for char in value)):
        raise ValueError('Invalid remote URL')
    _ = url.port
    if url.scheme == 'http' and not private_host(url.hostname):
        raise ValueError('HTTP requires a literal LAN/Tailscale IP or localhost; other endpoints require HTTPS')
    return url
