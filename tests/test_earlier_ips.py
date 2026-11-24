"""Run inside the NetBox container, see test_snmp_timeout.py."""
import unittest
from unittest import mock

import django

django.setup()

from netbox_lens.backends import netdisco  # noqa: E402

MAC = "70:d8:23:00:1f:bd"


def _resp(data):
    r = mock.Mock(ok=True, content=b"x", status_code=200)
    r.json.return_value = data
    r.raise_for_status.return_value = None
    return r


class EarlierIpsTest(unittest.TestCase):
    def _search(self, archived_rows, **kw):
        b = netdisco.NetdiscoBackend({"url": "http://nd"})
        active = {"mac": MAC, "ip": "10.1.2.3", "active": True, "time_last": "2026-11-18 10:25"}

        def get(url, params=None, **_):
            if url.endswith("/search/device"):
                return _resp([])
            if params.get("archived") == "true":
                return _resp({"ips": archived_rows})
            return _resp({"sightings": [{"mac": MAC, "port": "Gi1"}], "ips": [active]})

        with mock.patch.object(netdisco.requests, "get", side_effect=get) as g:
            r = b.search(MAC, **kw)
        return r, g

    def test_archived_ips_listed_newest_first_once(self):
        rows = [
            {"mac": MAC, "ip": "10.1.2.3", "active": True, "time_last": "2026-11-18 10:25"},
            {"mac": MAC, "ip": "10.1.2.77", "active": False, "time_last": "2026-11-02 08:00"},
            {"mac": MAC, "ip": "10.1.2.77", "active": False, "time_last": "2026-10-01 08:00"},
            {"mac": MAC, "ip": "10.9.9.9", "active": False, "time_last": "2026-11-10 08:00"},
        ]
        r, _ = self._search(rows)
        self.assertEqual([e["ip"] for e in r.earlier_ips], ["10.9.9.9", "10.1.2.77"])
        self.assertEqual(r.earlier_ips[1]["time_last"], "2026-11-02 08:00")

    def test_not_for_archived_searches(self):
        r, g = self._search([{"mac": MAC, "ip": "10.9.9.9", "active": False}], archived=True)
        self.assertEqual(r.earlier_ips, [])


if __name__ == "__main__":
    unittest.main()
