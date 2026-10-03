"""A node's public HTTPS address: what an operator advertises and what a
client agrees to follow.

Light-safe: imports nothing beyond the standard library.
"""

import ipaddress
from urllib.parse import urlsplit

# Names that mean this machine or its own network, whatever they resolve to
# in a certificate's eyes. A client follows addresses other nodes advertise,
# so it must not be steered to one of these.
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")


def parse_public_url(url):
    """The normalized https://host[:port] for url, or ValueError saying why
    it is not acceptable.

    Only a DNS name, never an IP address or a name that is plainly local: a
    certificate vouches for names, so an address with none cannot be
    encrypted to anyone in particular, and a client that follows a URL some
    node advertised must not be pointed at something on its own machine or
    network. No credentials, no path, no query.
    """
    if not isinstance(url, str):
        raise ValueError("it must be text")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ValueError("it must start with https://")
    if parts.username or parts.password:
        raise ValueError("it must not carry a login")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("it must be just the host, with no path")
    host = parts.hostname
    if not host:
        raise ValueError("it has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("it must be a name, not an IP address: a certificate is issued to a name")
    if "." not in host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise ValueError("it must be a public name")
    port = parts.port                     # ValueError if it is not a port
    return "https://" + host.lower() + (f":{port}" if port and port != 443 else "")
