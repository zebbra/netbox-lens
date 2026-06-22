import re

from django import forms

MAC_RE = re.compile(
    r'^([0-9A-Fa-f]{2}[:\-.]?){5}[0-9A-Fa-f]{2}$'
    r'|^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$'  # Cisco dotted
)
IP_RE = re.compile(
    r'^(\d{1,3}\.){3}\d{1,3}(/\d+)?$'           # IPv4 / CIDR
    r'|^[0-9a-fA-F:]+(/\d+)?$'                  # IPv6
)

SINCE_CHOICES = [
    ("now",    "Now"),
    ("week",   "7 days"),
    ("2weeks", "2 weeks"),
    ("month",  "1 month"),
]

class NodeSearchForm(forms.Form):
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
    since = forms.ChoiceField(
        choices=SINCE_CHOICES,
        required=False,
        initial="week",
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
