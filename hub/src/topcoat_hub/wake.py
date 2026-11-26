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
from typing import Any, Protocol

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


def wake_addresses(inventory: dict | None) -> list[str]:
    """The MAC addresses to wake a node by, from its last inventory: every wired interface
    (wireless ones only if there is no other; Wake-on-LAN over Wi-Fi rarely works)."""
    interfaces = ((inventory or {}).get("hardware") or {}).get("network") or []
    wired = [i for i in interfaces if i.get("kind") != "wifi"] or interfaces
    return [i["mac"] for i in wired if isinstance(i.get("mac"), str)]


def broadcasts(setting: str) -> list[str]:
    """``TOPCOAT_WOL_BROADCAST`` may name several addresses (comma-separated), e.g. the directed
    broadcast of each LAN a node sits in when the hub is in another subnet."""
    return [b.strip() for b in setting.split(",") if b.strip()] or ["255.255.255.255"]


async def send_to(sender: WakeSender, macs: list[str], setting: str, port: int) -> int:
    """Send the packet for every address to every broadcast; returns the number of packets."""
    sent = 0
    for mac in macs:
        for broadcast in broadcasts(setting):
            await sender.send(mac, broadcast, port)
            sent += 1
    return sent


def demand_waker(sessionmaker: Any, service: Any, sender: WakeSender, settings: Any) -> Any:
    """The gateway's waker (plan step 1.7): sends the packets for a node from its last inventory
    and records ``wake_sent`` with the reason ``demand``. Returns the number of packets (0: the
    hub knows no address of the node)."""

    async def wake(node_id: Any, model: str) -> int:
        from topcoat_hub.models import NodeInventory

        async with sessionmaker() as db:
            inventory = await db.get(NodeInventory, node_id)
            macs = wake_addresses(inventory.inventory if inventory else None)
        if not macs:
            return 0
        sent = await send_to(sender, macs, settings.wol_broadcast, settings.wol_port)
        data = {"reason": "demand", "model": model, "addresses": len(macs)}
        await service.record_event(node_id, "wake_sent", data)
        return sent

    return wake
