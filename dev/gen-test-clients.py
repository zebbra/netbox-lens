#!/usr/bin/env python3
"""
Insert synthetic client data into Netdisco for LENS testing.

Generates realistic-looking MAC/IP/DNS entries across multiple VLANs and
switch ports, using real vendor OUIs so manufacturer lookup works.

Usage:
    python3 gen-test-clients.py [--clean] [--count N]
    python3 gen-test-clients.py --clean   # remove only synthetic rows
"""

import argparse
import os
import random
import subprocess
import sys

CONTAINER = os.environ.get("NETDISCO_DB_CONTAINER", "lens-netdisco-postgresql")

# Real switch in the lab
SWITCH = "100.88.88.5"
SWITCH_PORTS = [
    ("GigabitEthernet1/0/3",  1,   "shield"),
    ("GigabitEthernet1/0/4",  1,   "cam"),
    ("GigabitEthernet1/0/5",  11,  "office"),
    ("GigabitEthernet1/0/6",  11,  "office"),
    ("GigabitEthernet1/0/9",  87,  "iot"),
    ("GigabitEthernet1/0/10", 87,  "iot"),
    ("GigabitEthernet1/0/11", 1,   "lab"),
    ("GigabitEthernet1/0/12", 1,   "lab"),
]

# OUI prefixes that exist in Netdisco's oui table
VENDORS = [
    ("3c:22:fb", "Apple"),
    ("18:66:da", "Dell"),
    ("00:50:56", "VMware"),
    ("b8:27:eb", "Raspberry Pi"),
    ("dc:a6:32", "Raspberry Pi"),
    ("00:0c:29", "VMware"),
    ("f0:9f:c2", "Ubiquiti"),
    ("74:83:c2", "Intel"),
    ("8c:8d:28", "Intel"),
    ("00:1a:2b", "Cisco"),
    ("fc:15:b4", "Apple"),
    ("a4:c3:f0", "Google"),
    ("54:27:1e", "Sonos"),
]

CLIENTS = [
    # (hostname, ip, ipv6, vlan, vendor_idx, note)
    ("macbook-stefan",    "192.168.1.10",  "2a02:1:dead::10",   1,  0,  "laptop"),
    ("macbook-lisa",      "192.168.1.11",  None,                1,  12, "laptop"),
    ("iphone-stefan",     "192.168.1.12",  None,                1,  0,  "phone"),
    ("dell-workstation",  "192.168.1.20",  "2a02:1:dead::20",   1,  1,  "desktop"),
    ("vmhost-01",         "192.168.1.30",  "2a02:1:dead::30",   1,  2,  "hypervisor"),
    ("vmhost-02",         "192.168.1.31",  None,                1,  5,  "hypervisor"),
    ("pi-cam-front",      "192.168.11.10", None,                11, 3,  "camera"),
    ("pi-cam-back",       "192.168.11.11", None,                11, 4,  "camera"),
    ("pi-doorbell",       "192.168.11.12", None,                11, 3,  "camera"),
    ("ubiquiti-ap-01",    "192.168.1.50",  "2a02:1:dead::50",   1,  6,  "ap"),
    ("ubiquiti-ap-02",    "192.168.1.51",  None,                1,  6,  "ap"),
    ("intel-nuc-01",      "192.168.1.60",  "2a02:1:dead::60",   1,  7,  "nuc"),
    ("intel-nuc-02",      "192.168.1.61",  None,                1,  8,  "nuc"),
    ("shelly-plug-01",    "192.168.87.10", None,                87, 9,  "iot"),
    ("shelly-plug-02",    "192.168.87.11", None,                87, 9,  "iot"),
    ("shelly-plug-03",    "192.168.87.12", None,                87, 9,  "iot"),
    ("tasmota-switch-01", "192.168.87.20", None,                87, 9,  "iot"),
    ("google-home-01",    "192.168.87.30", None,                87, 11, "smarthome"),
    ("google-home-02",    "192.168.87.31", None,                87, 11, "smarthome"),
    ("sonos-living",      "192.168.1.70",  None,                1,  12, "audio"),
    ("sonos-bedroom",     "192.168.1.71",  None,                1,  12, "audio"),
    # archived clients (seen in the past, no longer active)
    ("old-laptop",        "192.168.1.100", None,                1,  1,  "archived"),
    ("old-phone",         "192.168.1.101", None,                1,  0,  "archived"),
]

DOMAIN = "home.broccoli.rocks"
SYNTHETIC_TAG = "synthetic-test"


def _s(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def psql(sql):
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "netdisco",
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
    )
    if r.returncode != 0:
        print(r.stderr.decode(), file=sys.stderr)
        sys.exit(1)
    return r.stdout.decode()


def make_mac(oui_prefix, idx):
    return f"{oui_prefix}:00:{(idx >> 8) & 0xff:02x}:{idx & 0xff:02x}"


def port_for_vlan(vlan):
    candidates = [p for p in SWITCH_PORTS if p[1] == vlan]
    return random.choice(candidates or SWITCH_PORTS)


def clean():
    print("Removing synthetic rows…")
    psql(f"""
DELETE FROM node    WHERE switch = '{SWITCH}' AND port LIKE 'GigabitEthernet1/0/%' AND oui IN ({', '.join(_s(v[0]) for v in VENDORS)});
DELETE FROM node_ip WHERE dns LIKE '%.{DOMAIN}';
""")
    print("Done.")


def generate(count=None):
    clients = CLIENTS[:count] if count else CLIENTS
    node_rows = []
    node_ip_rows = []

    router_json = f"'\"{{\"{SWITCH}\": \"{{}}\"}}'::jsonb"  # placeholder, set per-row below

    for idx, (host, ipv4, ipv6, vlan, vendor_idx, note) in enumerate(clients):
        oui, _ = VENDORS[vendor_idx % len(VENDORS)]
        mac = make_mac(oui, idx + 1)
        dns = f"{host}.{DOMAIN}"
        port, _, _ = port_for_vlan(vlan)
        archived = note == "archived"
        active = "false" if archived else "true"
        time_first  = "LOCALTIMESTAMP - interval '30 days'" if archived else "LOCALTIMESTAMP - interval '7 days'"
        time_last   = "LOCALTIMESTAMP - interval '14 days'" if archived else "LOCALTIMESTAMP - interval '10 minutes'"
        time_recent = time_last

        node_rows.append(
            f"('{mac}', '{SWITCH}', {_s(port)}, '{vlan}', "
            f"'{oui}', {time_first}, {time_recent}, {time_last}, {active})"
        )

        seen_ts = "2026-06-21T20:00:00" if archived else "2026-06-21T22:00:00"
        seen_json = f"'\"{{\"{SWITCH}\": \"{seen_ts}\"}}'::jsonb"
        node_ip_rows.append(
            f"('{mac}', '{ipv4}', {active}, {time_first}, {time_last}, {_s(dns)}, "
            f"'{{\"{SWITCH}\": \"{seen_ts}\"}}'::jsonb, "
            f"'{{\"{SWITCH}\": \"{seen_ts}\"}}'::jsonb)"
        )
        if ipv6:
            node_ip_rows.append(
                f"('{mac}', '{ipv6}', {active}, {time_first}, {time_last}, {_s(dns)}, "
                f"'{{\"{SWITCH}\": \"{seen_ts}\"}}'::jsonb, "
                f"'{{\"{SWITCH}\": \"{seen_ts}\"}}'::jsonb)"
            )

    print(f"Inserting {len(clients)} clients ({len(node_ip_rows)} IP rows)…")

    psql(f"""
INSERT INTO node (mac, switch, port, vlan, oui, time_first, time_recent, time_last, active)
VALUES {', '.join(node_rows)}
ON CONFLICT (mac, switch, port, vlan) DO UPDATE SET
  time_recent = EXCLUDED.time_recent,
  time_last   = EXCLUDED.time_last,
  active      = EXCLUDED.active;
""")

    psql(f"""
INSERT INTO node_ip (mac, ip, active, time_first, time_last, dns, seen_on_router_first, seen_on_router_last)
VALUES {', '.join(node_ip_rows)}
ON CONFLICT (mac, ip, vrf) DO UPDATE SET
  dns                  = EXCLUDED.dns,
  time_last            = EXCLUDED.time_last,
  active               = EXCLUDED.active,
  seen_on_router_last  = EXCLUDED.seen_on_router_last;
""")

    print("Done. Sample MACs:")
    for idx, (host, ipv4, _, vlan, vendor_idx, note) in enumerate(clients[:5]):
        oui, vendor = VENDORS[vendor_idx % len(VENDORS)]
        mac = make_mac(oui, idx + 1)
        print(f"  {mac}  {ipv4:18s}  {host}.{DOMAIN}  ({vendor})")
    if len(clients) > 5:
        print(f"  … and {len(clients)-5} more")


def main():
    p = argparse.ArgumentParser(description="Generate synthetic Netdisco client data for LENS testing")
    p.add_argument("--clean", action="store_true", help="Remove synthetic rows and exit")
    p.add_argument("--count", type=int, help="Only insert first N clients")
    args = p.parse_args()

    if args.clean:
        clean()
    else:
        generate(args.count)


if __name__ == "__main__":
    main()
