#!/usr/bin/env python3
"""
Run discobox's real sync_device for ap-test-wlc1 against the local NetBox,
with a fake Netdisco that reports the seed-ap-test.py APs the way a Cisco
9800 WLC does (ap-class modules + "<radio MAC>.<slot>" radio ports; data
shape from bit-discobox/tests/samples/wlc9800-*.json, all values fake).

    python dev/seed-ap-test.py --reset && python dev/fake-wlc-sync.py
    (full round trip incl. /types/library: see seed-ap-test.py)

Imports discobox from ../../bit-discobox, so local edits are tested directly.
Housekeeping is on (removes the APs' main/vlan2 placeholders).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "bit-discobox"))

from discobox import NetboxClient, sync_device  # noqa: E402

WLC_IP = "192.0.2.250"
# name, model, serial, ethernet MAC, radio base MAC, radio slots (slot, Netdisco port type), tag
APS = [
    ("AP-TEST-W051", "C9120AXE-E", "FAK0000W051", "02:00:00:00:51:01", "02:00:00:00:51:a0", [(0, "dot11b"), (1, "dot11a")], "SITE-A/wrong"),
    ("AP-TEST-W052", "C9120AXE-E", "FAK0000W052", "02:00:00:00:52:01", "02:00:00:00:52:a0", [(0, "dot11b"), (1, "dot11a")], "SITE-A/dot1x"),
    ("AP-TEST-W053", "C9120AXE-E", "FAK0000W053", "02:00:00:00:53:01", "02:00:00:00:53:a0", [(0, "dot11b"), (1, "dot11a")], "SITE-A/wrong"),
    ("AP-TEST-W060", "C9120AXI-E", "FAK0000W060", "02:00:00:00:60:01", "02:00:00:00:60:a0", [(0, "dot11b"), (1, "dot11a")], "SITE-A/wrong"),
    # created after the library enrichment → already has template interfaces to adopt
    ("AP-TEST-W099", "C9120AXE-E", "FAK0000W099", "02:00:00:00:99:01", "02:00:00:00:99:a0", [(0, "dot11b"), (1, "dot11a")], "SITE-A/wrong"),
    ("AP-TEST-W062", "CW9166I-E", "FAK0000W062", "02:00:00:00:62:01", "02:00:00:00:62:a0", [(0, "dot11b"), (1, "dot11a"), (2, "3")], "SITE-A/dot1x"),
]


def _desc(name, model, eth, radio, tag) -> str:
    return (f"{model}: {name} ({tag}); IP 192.0.2.{int(radio[-5:-3], 16)}; "
            f"Dot3 MAC {radio}; Ethernet MAC {eth}; Connected via SW-TEST-X01.example.com")


class FakeNetdisco:
    def get_device(self, ip):
        return {"ip": WLC_IP, "name": "ap-test-wlc1", "dns": "ap-test-wlc1", "vendor": "cisco",
                "model": "C9800-L-C-K9", "serial": "FAK0000WLC1", "os_ver": "17.15.5",
                "description": "Cisco IOS Software [IOSXE], C9800 Software, Version 17.15.5"}

    def get_ports(self, ip):
        ports = [{"ip": WLC_IP, "port": "GigabitEthernet0/0/0", "descr": "GigabitEthernet0/0/0",
                  "name": "", "type": "ethernetCsmacd", "up": "up", "up_admin": "up",
                  "mac": "02:00:00:00:00:01", "speed": "1 Gbps", "mtu": 1500}]
        for name, model, serial, eth, radio, slots, tag in APS:
            for slot, rtype in slots:
                ports.append({"ip": WLC_IP, "port": f"{radio}.{slot}", "name": name, "type": rtype,
                              "mac": radio, "up": "up", "up_admin": "enable",
                              "descr": _desc(name, model, eth, radio, tag).split(": ", 1)[1].split(" (", 1)[1]})
        return ports

    def get_modules(self, ip):
        return [{"ip": WLC_IP, "class": "ap", "name": "AP", "model": model, "serial": serial,
                 "sw_ver": "17.15.5.36", "type": "ap9120AXE", "index": i, "parent": 1, "pos": i,
                 "description": _desc(name, model, eth, radio, tag)}
                for i, (name, model, serial, eth, radio, slots, tag) in enumerate(APS, start=10)]

    def get_device_ips(self, ip):
        return []

    def get_powered_ports(self, ip):
        return []

    def __getattr__(self, name):
        raise NotImplementedError(f"FakeNetdisco.{name}")


def _token() -> str:
    if os.getenv("NETBOX_TOKEN"):
        return os.environ["NETBOX_TOKEN"]
    for line in (HERE / "discobox.env").read_text().splitlines():
        if line.startswith("NETBOX_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("NETBOX_TOKEN not set and not in dev/discobox.env")


def main() -> int:
    logging.basicConfig(level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s", datefmt="%H:%M:%S")
    types_cfg = (yaml.safe_load((HERE / "discobox.yaml").read_text()) or {}).get("types") or {}
    nb = NetboxClient(
        url=os.getenv("NETBOX_URL_HOST", "http://localhost:8000"), token=_token(),
        device_type_aliases=types_cfg.get("device_aliases"),   # same aliases as the dev discobox, if any
    )
    result = sync_device(
        WLC_IP, FakeNetdisco(), nb, sync_mac=True, sync_ip=False, sync_modules=True,
        sync_sfp=False, sync_poe=False, housekeeping=True,
        cf_os_version=None, cf_os_name=None, cf_os_release=None, cf_stack_members=None, cf_touch=None,
        cf_neighbor_text=None, cf_neighbor_port=None, cf_neighbor_device=None, cf_neighbor_iface=None,
    )
    print("result:", {k: result.get(k) for k in ("ok", "hostname", "aps", "interfaces")})
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
