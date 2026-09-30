"""Literal local IPv4 address and port rules shared by the CLI, HTTP and charts.

Only ``ipaddress`` is imported, so the CLI may use these before it isolates
the environment. ``local_server.http`` re-exports ``_address``/``_port`` and
``local_server.cli`` keeps ``_private_ipv4`` as the same object.
"""

import ipaddress


PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_LOOPBACK = ipaddress.IPv4Address("127.0.0.1")


def private_ipv4(value):
    """True only for the exact dotted text of a private (RFC 1918) IPv4 address."""
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return str(address) == value and any(address in network for network in PRIVATE_NETWORKS)


def local_address(value):
    """Return the exact loopback or private IPv4 text, or raise a fixed ValueError."""
    if type(value) is not str:
        raise ValueError("A literal local IPv4 address is required.")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("A literal local IPv4 address is required.") from None
    if str(address) != value or not (
        address == _LOOPBACK
        or any(address in network for network in PRIVATE_NETWORKS)
    ):
        raise ValueError("A literal local IPv4 address is required.")
    return value


def local_port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("A valid local TCP port is required.")
    return value
