#!/usr/bin/env python3
"""
Push Linux router state into Netdisco via its API + direct DB writes.

Reads an Ansible facts cache file to populate device metadata and ports,
then pushes the ARP/NDP neighbor table via the arpnip/macsuck API.

Usage:
    push-neighbors.py --neighbors <file> --router-ip <ip> --facts-cache <path>
                      [--netdisco-url <url>] [--token <token>]

The DB registration step uses `docker exec` on lens-netdisco-postgresql
(override with NETDISCO_DB_CONTAINER env var).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONTAINER = os.environ.get("NETDISCO_DB_CONTAINER", "lens-netdisco-postgresql")

ACTIVE_STATES    = {"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"}
SKIP_MAC_PREFIXES = ("ff:", "33:33:", "01:")
SKIP_IFACES      = {"lo"}

_MAC_RE = re.compile(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$', re.I)
_IP_RE  = re.compile(r'^[0-9a-f:.]+$', re.I)
_DEV_RE = re.compile(r'^[a-zA-Z0-9._@-]{1,64}$')


# ── Ansible facts cache ───────────────────────────────────────────────────────

def load_facts(path: str) -> dict:
    if not os.path.exists(path):
        dirname, basename = os.path.split(path)
        alt = os.path.join(dirname, f"s1_{basename}")
        if os.path.exists(alt):
            print(f"Facts cache: using {alt} (s1_ prefix auto-detected)")
            path = alt
    with open(path) as f:
        data = json.load(f)
    payload = data.get("__payload__", "{}")
    return json.loads(payload) if isinstance(payload, str) else payload


def device_meta(facts: dict) -> dict:
    serial = facts.get("ansible_product_serial", "") or ""
    if serial.upper() in ("NA", "N/A", "NONE", ""):
        serial = ""
    return {
        "name":   facts.get("ansible_hostname")  or "",
        "dns":    facts.get("ansible_nodename")  or "",
        "vendor": facts.get("ansible_system_vendor") or "",
        "os":     facts.get("ansible_distribution") or "",
        "os_ver": facts.get("ansible_distribution_version") or "",
        "model":  facts.get("ansible_product_name") or "",
        "serial": serial,
    }


def iface_ports(facts: dict, router_ip: str) -> list[dict]:
    """Return rows ready for device_port upsert."""
    ports = []
    for iface in facts.get("ansible_interfaces", []):
        if iface in SKIP_IFACES:
            continue
        key  = f"ansible_{iface}".replace("-", "_")
        info = facts.get(key, {})
        if not info or info.get("type") not in ("ether", "bonding", "bridge"):
            continue

        speed_raw = info.get("speed", -1)
        speed     = str(speed_raw) if (speed_raw and speed_raw > 0) else None

        ports.append({
            "ip":       router_ip,
            "port":     iface,
            "name":     iface,
            "mac":      info.get("macaddress") or None,
            "mtu":      info.get("mtu") or None,
            "speed":    speed,
            "up":       "up" if info.get("active") else "down",
            "up_admin": "up",
            "type":     info.get("type") or None,
        })
    return ports


# ── SQL helpers ───────────────────────────────────────────────────────────────

def _s(value) -> str:
    """SQL string literal or NULL."""
    if value is None or value == "":
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _i(value) -> str:
    """SQL integer literal or NULL."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "NULL"


def psql(sql: str):
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "netdisco",
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
    )
    if result.returncode != 0:
        print(result.stderr.decode(), file=sys.stderr)
        sys.exit(1)


def register_device(router_ip: str, meta: dict):
    ip = re.sub(r"[^0-9a-f:.]", "", router_ip.lower())
    name = re.sub(r"[^a-zA-Z0-9._-]", "", meta.get("name", ""))[:64] or ip

    psql(f"""
INSERT INTO device (ip, name, dns, vendor, os, os_ver, model, serial, is_pseudo, layers)
VALUES (
  '{ip}', {_s(name)}, {_s(meta.get('dns'))},
  {_s(meta.get('vendor'))}, {_s(meta.get('os'))}, {_s(meta.get('os_ver'))},
  {_s(meta.get('model'))}, {_s(meta.get('serial'))},
  true, '00000110'
)
ON CONFLICT (ip) DO UPDATE SET
  name      = EXCLUDED.name,   dns    = EXCLUDED.dns,
  vendor    = EXCLUDED.vendor, os     = EXCLUDED.os,
  os_ver    = EXCLUDED.os_ver, model  = EXCLUDED.model,
  serial    = EXCLUDED.serial, is_pseudo = true,
  layers    = '00000110';

DELETE FROM device_skip WHERE device = '{ip}';
""")
    print(f"Device: {ip}  name={name}  os={meta.get('os')} {meta.get('os_ver')}  "
          f"vendor={meta.get('vendor') or '-'}")


def upsert_ports(ports: list[dict]):
    if not ports:
        return
    rows = ",\n  ".join(
        f"('{p['ip']}', {_s(p['port'])}, {_s(p['name'])}, "
        f"{_s(p['mac'])}, {_i(p['mtu'])}, {_s(p['speed'])}, "
        f"{_s(p['up'])}, {_s(p['up_admin'])}, {_s(p['type'])})"
        for p in ports
    )
    psql(f"""
INSERT INTO device_port (ip, port, name, mac, mtu, speed, up, up_admin, type)
VALUES
  {rows}
ON CONFLICT (port, ip) DO UPDATE SET
  name     = EXCLUDED.name,
  mac      = EXCLUDED.mac,
  mtu      = EXCLUDED.mtu,
  speed    = EXCLUDED.speed,
  up       = EXCLUDED.up,
  up_admin = EXCLUDED.up_admin,
  type     = EXCLUDED.type;
""")
    print(f"Ports:  {len(ports)} interfaces upserted "
          f"({', '.join(p['port'] for p in ports)})")


# ── API ───────────────────────────────────────────────────────────────────────

class _PutRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if "login" in newurl.lower():
            raise urllib.error.HTTPError(
                newurl, 401,
                "Netdisco redirected to login — token is missing or invalid",
                headers, fp,
            )
        return urllib.request.Request(
            newurl,
            data=req.data,
            headers={k: v for k, v in req.header_items()},
            method=req.get_method(),
        )

_opener = urllib.request.build_opener(_PutRedirectHandler)


def api(url: str, token: str, data: bytes | None = None, method: str | None = None):
    headers: dict = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=data,
        method=method or ("PUT" if data is not None else "GET"),
        headers=headers,
    )
    return _opener.open(req)


# ── Neighbor parsing ──────────────────────────────────────────────────────────

def parse_neighbors(neighbors: list) -> tuple[list, list]:
    arps, nodes = [], []
    for n in neighbors:
        lladdr = n.get("lladdr", "").strip().lower()
        dst    = n.get("dst", "").strip()
        dev    = n.get("dev", "").strip()
        states = set(n.get("state", []))

        if not (lladdr and dst and dev):
            continue
        if lladdr.startswith(SKIP_MAC_PREFIXES):
            continue
        if not (_MAC_RE.match(lladdr) and _IP_RE.match(dst) and _DEV_RE.match(dev)):
            continue

        arps.append({"mac": lladdr, "ip": dst})
        nodes.append({"port": dev, "vlan": 0, "mac": lladdr})

    return arps, nodes


# ── Main ──────────────────────────────────────────────────────────────────────

def write_textfile(path: str, router_ip: str, arp_count: int, node_count: int, success: bool):
    label = f'router="{router_ip}"'
    ts    = time.time()
    lines = [
        f'# HELP netdisco_push_last_run_timestamp_seconds Unix timestamp of the last push attempt',
        f'# TYPE netdisco_push_last_run_timestamp_seconds gauge',
        f'netdisco_push_last_run_timestamp_seconds{{{label}}} {ts:.0f}',
        f'# HELP netdisco_push_success Whether the last push succeeded (1=yes, 0=no)',
        f'# TYPE netdisco_push_success gauge',
        f'netdisco_push_success{{{label}}} {1 if success else 0}',
        f'# HELP netdisco_push_arp_entries Number of ARP/NDP entries pushed',
        f'# TYPE netdisco_push_arp_entries gauge',
        f'netdisco_push_arp_entries{{{label}}} {arp_count}',
        f'# HELP netdisco_push_node_entries Number of port/MAC node entries pushed',
        f'# TYPE netdisco_push_node_entries gauge',
        f'netdisco_push_node_entries{{{label}}} {node_count}',
    ]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description="Push router state into Netdisco")
    p.add_argument("--neighbors",    required=True, help="JSON from `ip -j neigh show` (v4+v6 combined)")
    p.add_argument("--router-ip",    required=True, help="Router management IP in Netdisco")
    p.add_argument("--facts-cache",  required=True, help="Ansible facts cache file for the router")
    p.add_argument("--textfile-dir", default="",    help="node_exporter textfile collector directory")
    p.add_argument("--netdisco-url", default=os.environ.get("NETDISCO_URL", "http://localhost:5001"))
    p.add_argument("--token",        default=os.environ.get("NETDISCO_TOKEN", ""))
    args = p.parse_args()

    if not args.token:
        p.error("no API token — pass --token or set NETDISCO_TOKEN")

    base_url  = args.netdisco_url.rstrip("/")
    router_ip = args.router_ip

    facts = load_facts(args.facts_cache)
    meta  = device_meta(facts)
    ports = iface_ports(facts, router_ip)

    with open(args.neighbors) as f:
        neighbors = json.load(f)

    arps, nodes = parse_neighbors(neighbors)
    if not arps:
        print("No valid neighbors found — nothing to push.")
        sys.exit(0)

    textfile = (
        os.path.join(args.textfile_dir, f"netdisco_push_{router_ip.replace(':', '_')}.prom")
        if args.textfile_dir else None
    )

    try:
        register_device(router_ip, meta)
        upsert_ports(ports)

        api(f"{base_url}/api/v1/object/device/{router_ip}/arps", args.token,
            data=json.dumps(arps).encode())
        print(f"ARP:    queued arpnip  with {len(arps)} entries")

        api(f"{base_url}/api/v1/object/device/{router_ip}/nodes", args.token,
            data=json.dumps(nodes).encode())
        print(f"Nodes:  queued macsuck with {len(nodes)} entries")

        if textfile:
            write_textfile(textfile, router_ip, len(arps), len(nodes), success=True)

    except Exception:
        if textfile:
            write_textfile(textfile, router_ip, 0, 0, success=False)
        raise


if __name__ == "__main__":
    main()
