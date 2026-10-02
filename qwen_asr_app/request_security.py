from __future__ import annotations

from urllib.parse import urlsplit


def _http_origin(value: str) -> tuple[str, str, int] | None:
    """Parse a serialized browser origin; reject credentials and URL suffixes."""
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


def websocket_origin_allowed(origin: str | None, request_url: str, additional_origins: str = "") -> bool:
    source = _http_origin(origin or "")
    if source is None:
        # Browsers always send Origin for this endpoint. Opaque or missing
        # origins must not attach to an existing microphone session.
        return False
    allowed = {parsed for value in additional_origins.split(",")
               if (parsed := _http_origin(value.strip())) is not None}
    try:
        request = urlsplit(request_url)
        scheme = {"ws": "http", "wss": "https"}.get(request.scheme)
        if scheme and request.hostname in {"localhost", "127.0.0.1", "::1"}:
            same_origin = _http_origin(f"{scheme}://{request.netloc}")
            if same_origin is not None:
                allowed.add(same_origin)
    except ValueError:
        return False
    return source in allowed


def http_request_allowed(request, additional_origins='', *, testing=False):
    """Protect the loopback server against cross-site writes and DNS rebinding."""
    current = _http_origin(str(request.base_url))
    configured = {_http_origin(value.strip()) for value in additional_origins.split(',') if value.strip()}
    configured.discard(None)
    local_hosts = {'localhost', '127.0.0.1', '::1'} | ({'test', 'testserver'} if testing else set())
    if current is None or (current[1] not in local_hosts and current not in configured):
        return False
    origin = request.headers.get('origin')
    if origin:
        source = _http_origin(origin)
        bridge = request.url.path.startswith('/api/course-streams') and request.headers.get('x-qwenasr-client') == 'smartclass'
        if source != current and source not in configured:
            if not (bridge and source and source[1] in local_hosts and source[2] == 8765):
                return False
    elif request.headers.get('sec-fetch-site') in {'cross-site', 'same-site'} and request.method not in {'GET', 'HEAD'}:
        return False
    return True
