"""Canonical portable deployment origin, independent of application libraries."""
import ipaddress
import re
from urllib.parse import urlsplit


def normalize_origin(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 512
            or any(c.isspace() or ord(c) < 32 or c in "%\\$" for c in value)):
        raise ValueError("Use one HTTPS or loopback HTTP origin")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not parsed.hostname.isascii() or (parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Use one HTTPS or loopback HTTP origin")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The installation port is invalid")
    host = parsed.hostname.lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", host.split(".")[-1])
                or len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                     for label in host.split("."))):
            raise ValueError("Use a valid DNS name or IP address for the installation")
    host = "[" + host + "]" if ":" in host else host
    suffix = ":" + str(port) if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else ""
    return parsed.scheme + "://" + host + suffix
