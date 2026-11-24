"""Run inside the NetBox container, see test_snmp_timeout.py."""
import unittest
from unittest import mock

import django

django.setup()

from netbox_lens.backends import netdisco  # noqa: E402
from netbox_lens import views  # noqa: E402


def _resp(data, status=200):
    r = mock.Mock(ok=status < 400, content=b"x", status_code=status)
    r.json.return_value = data
    return r


class IpHolderTest(unittest.TestCase):
    def setUp(self):
        self.b = netdisco.NetdiscoBackend({"url": "http://nd", "web_url": "http://ndweb"})

    def test_alias_of_other_device(self):
        holder = {"ip": "172.18.164.78", "name": "ALSBLE6-VCE1.nms.admin.ch", "last_discover": "2026-08-20 10:00"}
        with mock.patch.object(netdisco.requests, "get", return_value=_resp([holder])) as get:
            got = self.b.ip_holder("172.20.205.33")
        self.assertIn("search/device?ip=172.20.205.33", get.call_args.args[0])
        self.assertEqual(got["name"], "ALSBLE6-VCE1.nms.admin.ch")
        self.assertEqual(got["ip"], "172.18.164.78")
        self.assertIn("172.18.164.78", got["web_url"])

    def test_device_itself_or_nothing(self):
        for data in ([{"ip": "172.20.205.33", "name": "self"}], [], None):
            with mock.patch.object(netdisco.requests, "get", return_value=_resp(data)):
                self.assertIsNone(self.b.ip_holder("172.20.205.33"))
        with mock.patch.object(netdisco.requests, "get", return_value=_resp(None, 500)):
            self.assertIsNone(self.b.ip_holder("172.20.205.33"))


class NetboxHolderTest(unittest.TestCase):
    def test_cluster_member_in_netbox_is_not_a_conflict(self):
        from netbox_lens import template_content as tc
        member = mock.Mock(); member.name = "fw-a"; member.get_absolute_url.return_value = "/dcim/devices/7/"
        b = mock.Mock(label="Netdisco")
        b.ip_holder.return_value = {"name": "fw-a.example.ch", "ip": "10.0.0.2"}
        with mock.patch.object(tc, "NbDevice") as nb:
            nb.objects.filter.return_value.first.return_value = member
            self.assertEqual(tc._ip_holder([b], "10.0.0.1")["nb_url"], "/dcim/devices/7/")
            nb.objects.filter.return_value.first.return_value = None
            self.assertIsNone(tc._ip_holder([b], "10.0.0.1")["nb_url"])


class SkipReasonTest(unittest.TestCase):
    def test_ip_held_by_other_device(self):
        msg = views._skip_reason({"status": "skipped", "reason": "ip_held_by_other_device",
                                  "hostname": "ALSBLE6-VCE1", "netdisco_ip": "172.18.164.78"})
        self.assertIn("ALSBLE6-VCE1 (172.18.164.78)", msg)

    def test_bulk_entry_uses_netdisco_hostname(self):
        msg = views._skip_reason({"reason": "ip_held_by_other_device", "hostname": "sepmoe29-cx01",
                                  "netdisco_hostname": "ALSBLE6-VCE1", "netdisco_ip": "172.18.164.78"})
        self.assertIn("ALSBLE6-VCE1 (172.18.164.78)", msg)
        self.assertNotIn("sepmoe29", msg)

    def test_other_reasons_pass_through(self):
        self.assertEqual(views._skip_reason({"reason": "cooldown"}), "cooldown")
        self.assertIsNone(views._skip_reason(None))


if __name__ == "__main__":
    unittest.main()
