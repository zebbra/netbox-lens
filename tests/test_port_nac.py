"""Run inside the NetBox container, see test_snmp_timeout.py."""
import unittest
from unittest import mock

import django

django.setup()

from netbox_lens import template_content as tc  # noqa: E402

MAC = "70:d8:23:00:1f:bd"


class NacSessionTest(unittest.TestCase):
    def test_eapol_source_or_mab_user(self):
        self.assertTrue(tc._is_nac_session({"last_eapol_source": "70:D8:23:00:1F:BD"}, MAC))
        for user in ("70d8.2300.1fbd", "70-D8-23-00-1F-BD", "70D823001FBD"):
            self.assertTrue(tc._is_nac_session({"mab": "mab", "user": user}, MAC), user)
        self.assertFalse(tc._is_nac_session({"user": "70d8.2300.1fbd"}, MAC))  # not MAB
        self.assertFalse(tc._is_nac_session({"mab": "mab", "user": "00:11:22:33:44:55"}, MAC))
        self.assertFalse(tc._is_nac_session(None, MAC))


class ApplyPortNacTest(unittest.TestCase):
    def test_only_active_wired_edge_ports_one_call_each(self):
        backend = mock.Mock()
        backend.port_pae.return_value = {"authconfig_state": "authenticated", "port_status": "authorized",
                                         "user": "host/pc1", "last_eapol_source": MAC}
        rows = [
            {"mac": MAC, "switch": "10.0.0.2", "port": "Gi1/0/5", "active": True, "is_wireless": False},
            {"mac": "aa:bb:cc:dd:ee:ff", "switch": "10.0.0.2", "port": "Gi1/0/5", "active": True, "is_wireless": False},
            {"mac": MAC, "switch": "10.0.0.2", "port": "Gi1/0/6", "active": False, "is_wireless": False},
            {"mac": MAC, "switch": "10.0.0.1", "port": "a4:88:73:d2:d3:40.1", "active": True,
             "is_wireless": True, "port_device_url": "/dcim/devices/9/"},
            {"mac": MAC, "switch": "10.0.0.3", "port": "Te1/1/1", "active": True, "transit_via": "core1"},
        ]
        tc.apply_port_nac(rows, backend)
        backend.port_pae.assert_called_once_with("10.0.0.2", "Gi1/0/5")
        self.assertTrue(rows[0]["nac_session"])
        self.assertFalse(rows[1]["nac_session"])
        self.assertTrue(all("nac" not in r for r in rows[2:]))

    def test_port_without_nac(self):
        backend = mock.Mock()
        backend.port_pae.return_value = {}
        rows = [{"mac": MAC, "switch": "10.0.0.2", "port": "Gi1/0/5", "active": True, "is_wireless": False}]
        tc.apply_port_nac(rows, backend)
        self.assertNotIn("nac", rows[0])


if __name__ == "__main__":
    unittest.main()
