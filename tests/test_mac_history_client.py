"""Run inside the NetBox container, see test_snmp_timeout.py."""
import unittest
from unittest import mock

import django

django.setup()

from netbox_lens import mac_history  # noqa: E402

MAC = "00:50:56:a9:07:19"


class FakeBackend:
    def find_clients(self, query, partial=True):
        return [{"mac": MAC, "ip": "10.176.70.221", "dns": "astra-sb-a.astra.admin.ch"}]

    def resolve_mac(self, mac):
        # the MAC has another ARP entry that sorts first
        return {"ip": "10.179.16.44", "dns": "usntools-r.zas.admin.ch",
                "others": [{"ip": "10.176.70.221", "dns": "astra-sb-a.astra.admin.ch"}]}


class ClientSearchTest(unittest.TestCase):
    def test_shows_the_matched_entry_and_others(self):
        node = {"mac": MAC, "port": "Ethernet1/46", "_device_name": "sw", "_device_ip": "10.0.0.1"}
        with mock.patch.object(mac_history, "_fetch_sightings", return_value=[node]):
            rows, *_ = mac_history.build_mac_history([FakeBackend()], client_query="astra-sb")
        self.assertEqual(rows[0]["client_ip"], "10.176.70.221")
        self.assertEqual(rows[0]["client_name"], "astra-sb-a.astra.admin.ch")
        self.assertEqual([o["ip"] for o in rows[0]["client_others"]], ["10.179.16.44"])


if __name__ == "__main__":
    unittest.main()
