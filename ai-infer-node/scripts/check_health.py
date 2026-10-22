"""Fail installation unless the authenticated gateway reports verified readiness."""
import argparse
import ipaddress
import json
from pathlib import Path
import time
import tomllib
import urllib.error
import urllib.request


def wait_healthy(config, timeout=180):
    with Path(config).open('rb') as source:
        cfg = tomllib.load(source)['server']
    host = ipaddress.ip_address(cfg['host'])
    if host.is_unspecified:
        host = ipaddress.ip_address('127.0.0.1' if host.version == 4 else '::1')
    address = str(host) if host.version == 4 else '[' + str(host) + ']'
    token = Path(cfg['token_file']).expanduser().read_text().strip()
    request = urllib.request.Request(f"http://{address}:{cfg['port']}/v1/health",
                                     headers={'Authorization': 'Bearer ' + token})
    # Local readiness must not go through an operator's HTTP proxy or redirect
    # the private Bearer credential to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + timeout
    reason = 'not reachable'
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=min(10, max(.1, deadline - time.monotonic()))) as response:
                value = json.load(response)
            data = value.get('data', {})
            if (value.get('ok') is True and data.get('status') == 'healthy'
                    and data.get('startup_checks', {}).get('state') == 'verified'):
                return
            reason = 'startup verification incomplete'
        except (OSError, ValueError) as exc:
            reason = type(exc).__name__
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError('Gateway readiness failed: ' + reason + '; inspect journalctl -u ai-infer-gateway')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    wait_healthy(args.config, args.timeout)
    print('Gateway health verified; models and delegated worker scope checks passed.')
