"""URL normalization and public-destination checks, separate from HTTP parsing."""

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from .models import CollectionError

Resolver = Callable[[str], Sequence[str]]
_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl"}
_PRIVATE_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
)
_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


def normalize_url(url: str) -> str:
    """Normalize a public-looking HTTP URL without DNS or any network activity.

    Functional query parameters, their order, path case, and trailing slashes are
    preserved. Only known tracking parameters and fragments are removed.
    """
    if not isinstance(url, str) or len(url) > 2000:
        raise CollectionError(
            "Enter an HTTP(S) URL of at most 2,000 characters.", code="blocked_url"
        )
    url = url.strip()
    if not url or any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        raise CollectionError(
            "URLs cannot contain whitespace or control characters.", code="blocked_url", url=url
        )
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only public HTTP and HTTPS URLs are supported.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URLs containing credentials are not supported.")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
        if not host or "%" in host:
            raise ValueError("The URL hostname is invalid.")
        # Some network stacks accept abbreviated, decimal, hex, or octal IPv4
        # literals. Normalize them before the cheap private-address check too.
        if ":" not in host:
            try:
                host = str(ipaddress.IPv4Address(socket.inet_aton(host)))
            except OSError:
                pass
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (
                "." not in host
                or not _HOST.fullmatch(host)
                or host == "localhost"
                or host.endswith(_PRIVATE_SUFFIXES)
            ):
                raise ValueError("Local and private hostnames are not allowed.") from None
        else:
            if not _public_ip(str(address)):
                raise ValueError("Private, local, metadata, and reserved IP addresses are blocked.")
        authority = f"[{host}]" if ":" in host else host
        scheme = parsed.scheme.lower()
        if port is not None and port != (443 if scheme == "https" else 80):
            authority += f":{port}"
        # Re-encoding retained values can invalidate signed links, or change
        # service-specific distinctions such as %20 versus +. Decode keys only.
        query = "&".join(
            component
            for component in parsed.query.split("&")
            if not (key := unquote_plus(component.partition("=")[0]).lower()).startswith("utm_")
            and key not in _TRACKING
        )
        return urlunsplit((scheme, authority, parsed.path or "/", query, ""))
    except (ValueError, UnicodeError) as exc:
        raise CollectionError(str(exc), code="blocked_url", url=url) from exc


def system_resolver(hostname: str) -> list[str]:
    return list(
        dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        )
    )


def public_addresses(url: str, resolver: Resolver) -> list[str]:
    """Reject mixed public/private answers too; never select around a private answer."""
    host = urlsplit(url).hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = list(dict.fromkeys(resolver(host)))
        except (OSError, ValueError) as exc:
            raise CollectionError(
                "Could not resolve the source hostname.", code="network_error", url=url
            ) from exc
    else:
        addresses = [str(literal)]
    if not addresses or any(not _public_ip(value) for value in addresses):
        raise CollectionError(
            "The source resolves to a private or reserved address.", code="blocked_url", url=url
        )
    # Prefer IPv4 where both are offered; a vetted literal is pinned for the connection.
    return sorted(addresses, key=lambda value: ":" in value)
