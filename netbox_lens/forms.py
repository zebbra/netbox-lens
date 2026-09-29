import re
from datetime import date, timedelta

from django import forms

MAC_RE = re.compile(
    r'^([0-9A-Fa-f]{2}[:\-.]?){5}[0-9A-Fa-f]{2}$'
    r'|^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$'  # Cisco dotted
)
IP_RE = re.compile(
    r'^(\d{1,3}\.){3}\d{1,3}(/\d+)?$'           # IPv4 / CIDR
    r'|^[0-9a-fA-F:]+(/\d+)?$'                  # IPv6
)

# Fragments Netdisco can't match on their own: a wildcard search is a plain
# ILIKE on mac::text (always aa:bb:cc:dd:ee:ff), and it treats only complete
# IPv4 addresses / CIDRs as IPs. interpret_query rewrites these into a form
# it does match. Everything else searches as typed, with "*" / "%" as the
# user's own wildcards (there is no separate "partial" switch).
_IPV4_FRAGMENT_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})(?:\.(\d{1,3}))?\.?$")
_MAC_SEP_FRAGMENT_RE = re.compile(r"^[0-9a-f]{1,2}(?:[:-][0-9a-f]{0,2}){1,5}$", re.I)
_MAC_CISCO_FRAGMENT_RE = re.compile(r"^[0-9a-f]{4}(?:\.[0-9a-f]{0,4}){1,2}$", re.I)
# bare hex from 6 digits up: shorter ones are too likely a hostname fragment
_MAC_BARE_FRAGMENT_RE = re.compile(r"^[0-9a-f]{6,11}$", re.I)


def interpret_query(q):
    """Return (query, note) for a search string; note is None if unchanged.

    - 2-3 IPv4 octets ("10.0.0") -> CIDR ("10.0.0.0/24")
    - a MAC fragment in any notation -> "*<colon form>*". Only prefix-aligned
      fragments can be re-paired; a colon fragment from the middle
      ("45:58:f9") already works as typed.
    - anything else (full MACs/IPs, hostnames, vendors) as is

    Wildcards always go out as "*": Netdisco's sql_match only turns "*" into
    "%", and only a query it changed counts as a wildcard search. A typed "%"
    passes through unchanged, so a MAC search with it stays an exact match
    (hostnames happen to work because the DNS lookup appends ".%" itself).
    """
    if "*" in q or "%" in q:
        return q.replace("%", "*"), None
    m = _IPV4_FRAGMENT_RE.match(q)
    if m:
        octets = [int(o) for o in m.groups() if o is not None]
        if all(o <= 255 for o in octets):
            cidr = ".".join(str(o) for o in octets + [0] * (4 - len(octets))) + f"/{8 * len(octets)}"
            return cidr, cidr
    hexdigits = re.sub(r"[:.\-]", "", q).lower()
    if len(hexdigits) < 12:
        mac = None
        if _MAC_SEP_FRAGMENT_RE.match(q):
            mac = q.lower().replace("-", ":").strip(":")
        elif _MAC_CISCO_FRAGMENT_RE.match(q) or _MAC_BARE_FRAGMENT_RE.match(q):
            mac = ":".join(hexdigits[i:i + 2] for i in range(0, len(hexdigits), 2))
        if mac:
            return f"*{mac}*", f"*{mac}* (MAC fragment)"
    return q, None


def _week_ago():
    return date.today() - timedelta(days=7)


class DateRangeMixin(forms.Form):
    date_from = forms.DateField(
        label="From",
        required=False,
        initial=_week_ago,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-control"}),
    )
    date_to = forms.DateField(
        label="To",
        required=False,
        initial=date.today,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-control"}),
    )

    def clean(self):
        cleaned = super().clean()
        date_from, date_to = cleaned.get("date_from"), cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError('"From" date must not be after "To" date.')
        return cleaned


class NodeSearchForm(DateRangeMixin):
    q = forms.CharField(
        label="Search",
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-lg",
            "placeholder": "MAC · IP · hostname · vendor · device — * or % as wildcard",
            "autofocus": True,
            "autocomplete": "off",
            "spellcheck": "false",
        }),
    )

    def clean_q(self):
        q = self.cleaned_data["q"].strip()
        if not q:
            raise forms.ValidationError("Enter a MAC address, IP address, hostname, vendor, or device name.")
        # Skip MAC validation for IP addresses and wildcard searches
        if IP_RE.match(q) or "*" in q or "%" in q:
            return q
        # Shorter MAC fragments are fine (interpret_query turns them into a
        # wildcard search); only catch a MAC with too many digits.
        if (":" in q or "-" in q or "." in q) and not re.search(r'[a-zA-Z]{2,}', q):
            normalized = re.sub(r'[:\-\.]', '', q)
            if re.fullmatch(r'[0-9A-Fa-f]+', normalized) and len(normalized) > 12:
                raise forms.ValidationError(
                    f'"{q}" does not look like a valid MAC address. '
                    "Expected format: aa:bb:cc:dd:ee:ff"
                )
        return q

    def clean(self):
        cleaned = super().clean()
        q = cleaned.get("q")
        if q:
            query, note = interpret_query(q)
            cleaned["q"] = query
            cleaned["q_note"] = note
        return cleaned


class MacHistoryForm(DateRangeMixin):
    device = forms.CharField(
        label="Device",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Device name (partial)",
            "autocomplete": "off",
        }),
    )
    interface = forms.CharField(
        label="Interface",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. GigabitEthernet1/0/10 (partial)",
            "autocomplete": "off",
        }),
    )
    vlan = forms.CharField(
        label="VLAN",
        max_length=10,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. 88",
            "autocomplete": "off",
        }),
    )
    mac = forms.CharField(
        label="MAC",
        max_length=64,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "aa:bb:cc:dd:ee:ff",
            "autocomplete": "off",
        }),
    )
    client = forms.CharField(
        label="Client",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Client IP or hostname",
            "autocomplete": "off",
        }),
    )

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(f) for f in ("device", "interface", "vlan", "mac", "client")):
            raise forms.ValidationError("Enter at least one filter to search.")
        if cleaned.get("vlan") and not cleaned["vlan"].isdigit():
            raise forms.ValidationError("VLAN must be numeric.")
        return cleaned


class ArpHistoryForm(DateRangeMixin):
    mac = forms.CharField(
        label="MAC",
        max_length=64,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "aa:bb:cc:dd:ee:ff",
            "autocomplete": "off",
        }),
    )
    client = forms.CharField(
        label="Client",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Client IP or hostname",
            "autocomplete": "off",
        }),
    )
    device = forms.CharField(
        label="Router",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Router name (partial)",
            "autocomplete": "off",
        }),
    )

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(f) for f in ("mac", "client")):
            raise forms.ValidationError("Enter a MAC or client IP/hostname to search.")
        return cleaned


ADMIN_CHOICES = [
    ("", "Any"),
    ("up", "Up"),
    ("down", "Down"),
]


class InterfaceSearchForm(forms.Form):
    device = forms.CharField(
        label="Element name",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "Device name (partial)",
            "autocomplete": "off",
        }),
    )
    interface = forms.CharField(
        label="Interface (ifDescr)",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "e.g. 1/0/10 (partial)",
            "autocomplete": "off",
        }),
    )
    description = forms.CharField(
        label="Description (ifAlias)",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "Description (partial)",
            "autocomplete": "off",
        }),
    )
    vlan = forms.CharField(
        label="VLAN",
        max_length=10,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "e.g. 88",
            "autocomplete": "off",
        }),
    )
    speed = forms.CharField(
        label="Speed",
        max_length=32,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "placeholder": "e.g. 1 Gbit",
            "autocomplete": "off",
        }),
    )
    managed = forms.CharField(
        label="Managed",
        max_length=64,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-sm",
            "autocomplete": "off",
        }),
    )
    admin = forms.ChoiceField(
        label="If admin.",
        choices=ADMIN_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    # Sourced from VictoriaMetrics (interfaceUpDownState), not NetBox itself —
    # NetBox only tracks admin/enabled state, not operational state.
    oper = forms.ChoiceField(
        label="If oper.",
        choices=ADMIN_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("vlan") and not cleaned["vlan"].isdigit():
            raise forms.ValidationError("VLAN must be numeric.")
        return cleaned


class NacStatusForm(forms.Form):
    device = forms.CharField(
        label="Device",
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Device name (partial)",
            "autocomplete": "off",
        }),
    )
    interface = forms.CharField(
        label="Interface",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. 1/0/10 (partial)",
            "autocomplete": "off",
        }),
    )
    # Opt-in (not opt-out): unchecked checkboxes aren't submitted at all, so
    # Django can't tell "explicitly unchecked" from "not in this request" —
    # any link/reload without the param would silently reset an opt-out
    # "hide_disconnected" default back to False. Defaulting this to False
    # (hide disconnected) needs no special-casing since that's just what an
    # absent/unchecked checkbox already means.
    show_disconnected = forms.BooleanField(
        label="Show disconnected",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
