"""Run inside the NetBox container, see test_snmp_timeout.py."""
import unittest
from unittest import mock

import django

django.setup()

from netbox_lens.backends import netdisco  # noqa: E402

MAC = "00:50:56:a9:07:19"


def _resp(data):
    r = mock.Mock(ok=True, content=b"x", status_code=200)
    r.json.return_value = data
    r.raise_for_status.return_value = None
    return r


class SharedMacTest(unittest.TestCase):
    def test_hostname_match_flags_mac_with_many_ips(self):
        b = netdisco.NetdiscoBackend({"url": "http://nd"})
        hit = {"mac": MAC, "ip": "10.176.70.221", "dns": "astra-sb-a"}
        followup = {"sightings": [], "ips": [{"mac": MAC, "ip": f"10.0.0.{i}"} for i in range(25)]}

        def get(url, params=None, **kw):
            if url.endswith("/search/device"):
                return _resp([])
            return _resp(followup if params["q"] == MAC else {"macs": [hit]})

        with mock.patch.object(netdisco.requests, "get", side_effect=get):
            r = b.search("astra-sb")
        self.assertTrue(r.ips[0]["shared_mac"])
        self.assertEqual(r.ips[0]["mac_ip_count"], 25)


if __name__ == "__main__":
    unittest.main()
