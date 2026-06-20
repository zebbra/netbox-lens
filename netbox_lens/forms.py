import re

from django import forms

MAC_RE = re.compile(
    r'^([0-9A-Fa-f]{2}[:\-.]?){5}[0-9A-Fa-f]{2}$'
    r'|^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$'  # Cisco dotted
)


class NodeSearchForm(forms.Form):
    q = forms.CharField(
        label="Search",
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control form-control-lg",
            "placeholder": "MAC address · IP address · hostname",
            "autofocus": True,
            "autocomplete": "off",
            "spellcheck": "false",
        }),
    )
    archived = forms.BooleanField(
        required=False,
        label="Include history",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_q(self):
        q = self.cleaned_data["q"].strip()
        if not q:
            raise forms.ValidationError("Enter a MAC address, IP address, or hostname.")
        # Catch obvious MAC typos early (wrong length, invalid chars)
        # Only validate if it looks like an attempted MAC (contains colons/dashes)
        if (":" in q or "-" in q or "." in q) and not re.search(r'[a-zA-Z]{2,}', q):
            normalized = re.sub(r'[:\-\.]', '', q)
            if len(normalized) != 12 or not re.fullmatch(r'[0-9A-Fa-f]+', normalized):
                raise forms.ValidationError(
                    f'"{q}" does not look like a valid MAC address. '
                    "Expected format: aa:bb:cc:dd:ee:ff"
                )
        return q
