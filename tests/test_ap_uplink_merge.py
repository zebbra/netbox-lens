"""Run inside the NetBox container, see test_snmp_timeout.py."""
import unittest
from unittest import mock

import django

django.setup()

from netbox_lens import template_content as tc  # noqa: E402

MAC = "70:d8:23:00:1f:bd"


def _wlc(**kw):
    return {"mac": MAC, "switch": "10.0.0.1", "device": {"name": "BERFLL15A-WLC2.nms.admin.ch"},
            "port": "a4:88:73:d2:d3:40.1", "port_description": "zoleic-w118.nms.admin.ch",
            "port_device_url": "/dcim/devices/9/", "port_device_id": 9, "is_wireless": True,
            "time_first": "2026-11-18 10:25:01", "time_last": "2026-11-18 10:25:40", **kw}


def _switch(**kw):
    return {"mac": MAC, "switch": "10.0.0.2", "device": {"name": "ZOLEIC-X353.nms.admin.ch"},
            "port": "TenGigabitEthernet1/0/2", "port_description": "to ZOLEIC-W118",
            "nb_device_url": "/dcim/devices/5/", "nb_interface_url": "/dcim/interfaces/77/",
            "is_wireless": False, "time_first": "2026-11-18 10:25:03", "time_last": "2026-11-18 10:25:44", **kw}


class MergeApUplinksTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(tc, "NbIPAddress", None)
        p.start()
        self.addCleanup(p.stop)

    def test_switch_port_folds_into_wlc_row_by_description(self):
        rows = [_wlc(), _switch()]
        tc.merge_ap_uplinks(rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["via_switch"], "ZOLEIC-X353.nms.admin.ch")
        self.assertEqual(rows[0]["via_port"], "TenGigabitEthernet1/0/2")
        self.assertEqual(rows[0]["via_port_url"], "/dcim/interfaces/77/")

    def test_vlan_taken_from_switch_when_wlc_has_none(self):
        rows = [_wlc(vlan="0"), _switch(vlan="720")]
        tc.merge_ap_uplinks(rows)
        self.assertEqual(rows[0]["vlan"], "720")
        self.assertTrue(rows[0]["vlan_from_switch"])
        rows = [_wlc(vlan="30"), _switch(vlan="720")]
        tc.merge_ap_uplinks(rows)
        self.assertEqual(rows[0]["vlan"], "30")

    def test_other_ap_or_other_time_stays(self):
        for other in (_switch(port_description="to ZOLEIC-W1180"),
                      _switch(port_description="to ZOLEIC-W119"),
                      _switch(time_first="2026-11-17 08:00:00", time_last="2026-11-17 09:00:00"),
                      _switch(mac="00:11:22:33:44:55")):
            rows = [_wlc(), other]
            tc.merge_ap_uplinks(rows)
            self.assertEqual(len(rows), 2, other)
            self.assertNotIn("via_switch", rows[0])

    def test_mac_history_row_shape(self):
        sw = _switch()
        del sw["device"], sw["switch"]
        sw.update(device_name="ZOLEIC-X353.nms.admin.ch", device_ip="10.0.0.2")
        rows = [_wlc(), sw]
        tc.merge_ap_uplinks(rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["via_switch"], "ZOLEIC-X353.nms.admin.ch")

    def test_match_by_neighbor_ip(self):
        ip = mock.Mock()
        ip.ip = "10.9.9.9"
        with mock.patch.object(tc, "NbIPAddress") as nb:
            nb.objects.filter.return_value.values_list.return_value = [(ip, 9)]
            rows = [_wlc(), _switch(port_description="uplink AP", port_remote_ip="10.9.9.9")]
            tc.merge_ap_uplinks(rows)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
