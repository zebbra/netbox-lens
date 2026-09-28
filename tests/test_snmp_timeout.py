"""Run inside the NetBox container (netbox_lens imports netbox.plugins):
    cd /opt/netbox/netbox && DJANGO_SETTINGS_MODULE=netbox.settings \
        /opt/netbox/venv/bin/python -m unittest discover -s /opt/netbox/netbox-lens/tests -t /opt/netbox/netbox-lens
"""
import unittest

import django

django.setup()

from netbox_lens.backends.netdisco import discover_snmp_timeout_us, parse_snmp_timeout_us  # noqa: E402


class ParseSnmpTimeoutTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_snmp_timeout_us("20s"), 20_000_000)
        self.assertEqual(parse_snmp_timeout_us("2m"), 120_000_000)
        self.assertEqual(parse_snmp_timeout_us("500ms"), 500_000)

    def test_bare_number_is_seconds(self):
        self.assertEqual(parse_snmp_timeout_us("30"), 30_000_000)

    def test_empty_and_garbage(self):
        for value in (None, "", "garbage", "5 minutes"):
            self.assertIsNone(parse_snmp_timeout_us(value), value)


class DiscoverSnmpTimeoutTest(unittest.TestCase):
    def test_scrape_budget_twelfth_clamped(self):
        self.assertEqual(discover_snmp_timeout_us("2m"), 10_000_000)   # 10s
        self.assertEqual(discover_snmp_timeout_us("5m"), 20_000_000)   # 25s -> cap 20s
        self.assertEqual(discover_snmp_timeout_us("20s"), 10_000_000)  # 1.7s -> floor 10s
        self.assertEqual(discover_snmp_timeout_us("3m"), 15_000_000)   # 15s, in range

    def test_bare_number_is_seconds(self):
        self.assertEqual(discover_snmp_timeout_us("180"), 15_000_000)

    def test_empty_and_garbage_fall_back_to_floor(self):
        for value in (None, "", "garbage"):
            self.assertEqual(discover_snmp_timeout_us(value), 10_000_000, value)


if __name__ == "__main__":
    unittest.main()
