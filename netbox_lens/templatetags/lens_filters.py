from datetime import datetime

from django import template
from django.utils import timezone
from django.utils.timesince import timesince
from django.utils.translation import gettext as _

register = template.Library()


def _parse(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


@register.filter
def lens_relative(value):
    dt = _parse(value)
    if not dt:
        return value
    return _("%(time)s ago") % {"time": timesince(dt)}


@register.filter
def lens_absolute(value):
    dt = _parse(value)
    if not dt:
        return value
    return dt.strftime("%Y-%m-%d %H:%M")
