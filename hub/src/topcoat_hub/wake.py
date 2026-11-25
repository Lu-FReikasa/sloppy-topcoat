"""Wake-on-LAN (E2-05 E): the hub wakes a node whose machine sleeps or is off.

A "magic packet" is 6 bytes of 0xFF followed by the MAC address 16 times, sent as a UDP
broadcast (port 9) into the node's LAN. The hub learns the addresses from the node's inventory
(``hardware.network``), which it keeps while the node is offline.

In a container the broadcast only reaches the LAN if the hub runs with host networking, or if
``TOPCOAT_WOL_BROADCAST`` names the LAN's broadcast address and the container network routes it;
see docs/INSTALL-NODE.md "Energy".
"""

import asyncio
import re
import socket
from typing import Protocol

MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")


def magic_packet(mac: str) -> bytes:
    """The magic packet for ``mac`` (``aa:bb:cc:dd:ee:ff``, ``-`` also accepted)."""
    if not MAC_RE.match(mac):
        raise ValueError(f"not a MAC address: {mac!r}")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    return b"\xff" * 6 + raw * 16


class WakeSender(Protocol):
    async def send(self, mac: str, broadcast: str, port: int) -> None: ...


class UdpWakeSender:
    """Sends the packet as a UDP broadcast."""

    async def send(self, mac: str, broadcast: str, port: int) -> None:
        packet = magic_packet(mac)

        def _send() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(5)
                sock.sendto(packet, (broadcast, port))

        await asyncio.get_running_loop().run_in_executor(None, _send)
