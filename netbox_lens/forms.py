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
            "placeholder": "MAC · IP · hostname · vendor · device",
            "autofocus": True,
            "autocomplete": "off",
            "spellcheck": "false",
        }),
    )
    partial = forms.BooleanField(
        required=False,
        label="Partial match",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_q(self):
        q = self.cleaned_data["q"].strip()
        if not q:
            raise forms.ValidationError("Enter a MAC address, IP address, hostname, vendor, or device name.")
        # Skip MAC validation for IP addresses or when partial match is on
        if IP_RE.match(q) or self.data.get("partial"):
            return q
        # Catch obvious MAC typos (wrong length / invalid chars)
        # Only fires when input has separators but no letters (looks like attempted MAC)
        if (":" in q or "-" in q or "." in q) and not re.search(r'[a-zA-Z]{2,}', q):
            normalized = re.sub(r'[:\-\.]', '', q)
            if len(normalized) != 12 or not re.fullmatch(r'[0-9A-Fa-f]+', normalized):
                raise forms.ValidationError(
                    f'"{q}" does not look like a valid MAC address. '
                    "Expected format: aa:bb:cc:dd:ee:ff"
                )
        return q


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
