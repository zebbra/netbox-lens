"""Run inside the NetBox container, see test_snmp_timeout.py."""
import unittest

import django

django.setup()

from netbox_lens.forms import interpret_query  # noqa: E402


class InterpretQueryTest(unittest.TestCase):
    def test_ipv4_fragment_becomes_cidr(self):
        self.assertEqual(interpret_query("10.0.0"), ("10.0.0.0/24", "10.0.0.0/24"))
        self.assertEqual(interpret_query("10.0"), ("10.0.0.0/16", "10.0.0.0/16"))
        self.assertEqual(interpret_query("10.0.0."), ("10.0.0.0/24", "10.0.0.0/24"))

    def test_full_ip_and_cidr_unchanged(self):
        for q in ("10.0.0.5", "10.0.0.0/24"):
            self.assertEqual(interpret_query(q), (q, None))

    def test_invalid_octet_not_an_ip(self):
        self.assertEqual(interpret_query("300.1")[0], "300.1")

    def test_mac_fragments_to_colon_form(self):
        for q in ("78:45:58:f9:40", "78:45:58:F9:40:", "78-45-58-f9-40", "7845.58f9.40", "784558f940"):
            self.assertEqual(interpret_query(q)[0], "*78:45:58:f9:40*", q)

    def test_odd_bare_fragment(self):
        self.assertEqual(interpret_query("784558f94")[0], "*78:45:58:f9:4*")

    def test_middle_colon_fragment_as_typed(self):
        self.assertEqual(interpret_query("45:58:f9")[0], "*45:58:f9*")

    def test_full_macs_and_names_unchanged(self):
        for q in ("78:45:58:f9:40:96", "7845.58f9.4096", "784558f94096", "sw1", "cafe", "Cisco", "sw-e10"):
            self.assertEqual(interpret_query(q), (q, None), q)

    def test_own_wildcards_sent_as_star(self):
        self.assertEqual(interpret_query("cm02530%.adb.intra.admin.ch"), ("cm02530*.adb.intra.admin.ch", None))
        self.assertEqual(interpret_query("78:45:58%"), ("78:45:58*", None))
        self.assertEqual(interpret_query("cm0253*"), ("cm0253*", None))


if __name__ == "__main__":
    unittest.main()
