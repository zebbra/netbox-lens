#!/usr/bin/env python3
"""
Seed the local NetBox with a small, fake wireless-AP test set for discobox
(AP enrichment during WLC sync, /types/library DeviceType enrichment).

Idempotent: re-running only creates what's missing. --reset first deletes
everything this script owns (devices ap-test-*, their DeviceTypes incl.
templates, the test rack). The AP types mirror prod (2026-09): the WLC sync
finds them by part_number; a model without a matching type keeps its type
and is logged as "no DeviceType for model(s)" (fix the data in NetBox).

  - 9120AX              part_number C9120AXE-E (bossy name; the library has
                        no AXE entry, /types/library maps it to C9120AXI-E)
  - Catalyst 9120AXI-E  part_number C9120AXI-E, library model name
  - Catalyst CW9166I-E  part_number CW9166I-E, not in the library
  - ap-test-w051/w052   9120AX, unracked, neops-style comments, placeholder
                        interfaces main + vlan2
  - ap-test-w053        9120AX, racked (guards u_height)
  - ap-test-w060        Catalyst 9120AXI-E
  - ap-test-w062        Catalyst CW9166I-E
  - ap-test-w099        9120AX, only created once 9120AX has interface
                        templates (i.e. after /types/library apply): tests
                        that the WLC sync adopts template interfaces
  - ap-test-wlc1        the controller (role lwapp-ctr) for fake-wlc-sync.py

Full round trip:
    python dev/seed-ap-test.py --reset
    curl -X POST 'localhost:8081/types/library?role=lwapp-ap&apply=true'
    python dev/seed-ap-test.py            # adds ap-test-w099
    python dev/fake-wlc-sync.py

Token/URL come from dev/discobox.env (NETBOX_TOKEN); URL defaults to
http://localhost:8000. Needs pynetbox.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pynetbox

HERE = Path(__file__).resolve().parent
PREFIX = "ap-test-"
RACK = "rack-ap-test"
# model: (slug, part_number)
TYPES = {
    "9120AX": ("9120ax", "C9120AXE-E"),
    "Catalyst 9120AXI-E": ("catalyst-9120axi-e", "C9120AXI-E"),
    "Catalyst CW9166I-E": ("catalyst-cw9166i-e", "CW9166I-E"),
    "AP-TEST-WLC": ("ap-test-wlc", None),
}
NEOPS_COMMENTS = (
    "<!--- DO NOT EDIT BELOW -->\n"
    "<!--- neops added Sync infos, can be removed if fixed or project is accomplished  --->\n"
    "## Sync Infos\n - Room: -2.256 TEST id:1\n"
    "<!--- DO NOT EDIT ABOVE -->"
)
DEVICES = [
    # name,               type,      serial,        racked
    ("ap-test-w051", "9120AX", "FAK0000W051", False),
    ("ap-test-w052", "9120AX", "FAK0000W052", False),
    ("ap-test-w053", "9120AX", "FAK0000W053", True),
    ("ap-test-w060", "Catalyst 9120AXI-E", "FAK0000W060", False),
    ("ap-test-w062", "Catalyst CW9166I-E", "FAK0000W062", False),
]
LATE_DEVICE = ("ap-test-w099", "9120AX", "FAK0000W099")
LEFTOVER_TYPE_SLUGS = ("c9120axe-e", "c9120axi-e", "cw9166i-e", "cw9166i")
WLC = ("ap-test-wlc1", "AP-TEST-WLC", "FAK0000WLC1")


def _env_token() -> str:
    if os.getenv("NETBOX_TOKEN"):
        return os.environ["NETBOX_TOKEN"]
    for line in (HERE / "discobox.env").read_text().splitlines():
        if line.startswith("NETBOX_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("NETBOX_TOKEN not set and not in dev/discobox.env")


def _get_or_create(endpoint, lookup: dict, **create):
    hit = endpoint.get(**lookup)
    if hit:
        return hit, False
    return endpoint.create(**lookup, **create), True


def reset(nb) -> None:
    for dev in nb.dcim.devices.filter(name__isw=PREFIX):
        dev.delete()
        print(f"deleted device {dev.name}")
    for slug, _ in TYPES.values():
        dt = nb.dcim.device_types.get(slug=slug)
        if dt:
            dt.delete()        # cascades to its component templates
            print(f"deleted device type {dt.model}")
    # bare types an older discobox minted from AP models (pre-1.6 behaviour)
    for slug in LEFTOVER_TYPE_SLUGS:
        dt = nb.dcim.device_types.get(slug=slug)
        if dt:
            dt.delete()
            print(f"deleted leftover device type {dt.model}")
    rack = nb.dcim.racks.get(name=RACK)
    if rack:
        rack.delete()
        print(f"deleted rack {RACK}")


def seed(nb) -> None:
    mfr = next((m for m in nb.dcim.manufacturers.all() if m.name.lower() == "cisco"), None) \
        or nb.dcim.manufacturers.create(name="Cisco", slug="cisco")
    role, _ = _get_or_create(nb.dcim.device_roles, {"slug": "lwapp-ap"}, name="LWAPP_AP", color="2196f3")
    site, _ = _get_or_create(nb.dcim.sites, {"slug": "home"}, name="home")
    ctr_role, _ = _get_or_create(nb.dcim.device_roles, {"slug": "lwapp-ctr"}, name="LWAPP_CTR", color="3f51b5")
    rack, _ = _get_or_create(nb.dcim.racks, {"name": RACK}, site=site.id, u_height=10)
    types = {}
    for model, (slug, part_number) in TYPES.items():
        types[model], created = _get_or_create(
            nb.dcim.device_types, {"slug": slug}, model=model, manufacturer=mfr.id, u_height=1,
            **({"part_number": part_number} if part_number else {}),
        )
        if created:
            print(f"created device type {model}")
    for name, model, serial, racked in DEVICES:
        extra = {"rack": rack.id, "position": 1, "face": "front"} if racked else {}
        dev, created = _get_or_create(
            nb.dcim.devices, {"name": name},
            device_type=types[model].id, role=role.id, site=site.id, serial=serial,
            status="active", comments=NEOPS_COMMENTS, **extra,
        )
        if not created:
            continue
        print(f"created device {name}{' (racked)' if racked else ''}")
        for iface in ("main", "vlan2"):
            nb.dcim.interfaces.create(device=dev.id, name=iface, type="virtual")
    name, model, serial = LATE_DEVICE
    if nb.dcim.interface_templates.count(device_type_id=types[model].id):
        _, created = _get_or_create(
            nb.dcim.devices, {"name": name},
            device_type=types[model].id, role=role.id, site=site.id, serial=serial, status="active",
        )
        if created:
            print(f"created device {name} (from {model} templates)")
    name, model, serial = WLC
    _, created = _get_or_create(
        nb.dcim.devices, {"name": name},
        device_type=types[model].id, role=ctr_role.id, site=site.id, serial=serial, status="active",
    )
    if created:
        print(f"created device {name}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--reset", action="store_true", help="delete the test set first")
    p.add_argument("--url", default=os.getenv("NETBOX_URL_HOST", "http://localhost:8000"))
    args = p.parse_args()
    nb = pynetbox.api(args.url, token=_env_token())
    if args.reset:
        reset(nb)
    seed(nb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
