import re
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django import template
from django.utils import timezone
from django.utils.timesince import timesince
from django.utils.translation import gettext as _

register = template.Library()

# Past this, a relative "x ago" stops being useful and the date alone is enough.
RELATIVE_MAX_AGE = timedelta(days=7)


def _parse(value):
    """Return (aware datetime, has_time) for an ISO string, epoch number,
    date or datetime — device.cf hands date/datetime CFs over as objects,
    not strings. (None, False) if it isn't a timestamp at all."""
    if not value:
        return None, False
    has_time = True
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt, has_time = datetime.combine(value, time.min), False
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=dt_timezone.utc)
        else:
            text = str(value).strip()
            dt = datetime.fromisoformat(text)
            has_time = len(text) > 10
    except (TypeError, ValueError, OSError, OverflowError):
        return None, False
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return timezone.localtime(dt), has_time


@register.filter
def lens_relative(value):
    """Compact age: "3 hours ago" within the last week, the plain date beyond."""
    dt, has_time = _parse(value)
    if not dt:
        return value
    now = timezone.localtime()
    if not has_time:
        days = (now.date() - dt.date()).days
        if days == 0:
            return _("today")
        if days == 1:
            return _("yesterday")
        if 1 < days <= RELATIVE_MAX_AGE.days:
            return _("%(days)d days ago") % {"days": days}
        return dt.strftime("%Y-%m-%d")
    if timedelta(0) <= now - dt <= RELATIVE_MAX_AGE:
        return _("%(time)s ago") % {"time": timesince(dt, now, depth=1)}
    return dt.strftime("%Y-%m-%d")


@register.filter
def lens_absolute(value):
    """Exact timestamp for the hover, in local time; date only for date values."""
    dt, has_time = _parse(value)
    if not dt:
        return value or ""
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z" if has_time else "%Y-%m-%d")


@register.filter
def lens_datetime(value):
    """Date and minute for table cells, in local time; date only for date values."""
    dt, has_time = _parse(value)
    if not dt:
        return value or ""
    return dt.strftime("%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d")


@register.filter
def lens_ago(value):
    """Always-relative age for the hover ("3 weeks, 2 days ago"), no cut-off."""
    dt, has_time = _parse(value)
    if not dt:
        return ""
    now = timezone.localtime()
    if not has_time:
        days = (now.date() - dt.date()).days
        if days == 0:
            return _("today")
        if days == 1:
            return _("yesterday")
    if dt > now:
        return ""
    return _("%(time)s ago") % {"time": timesince(dt, now, depth=2)}


@register.simple_tag
def lens_age_axis(rows):
    """Shared time axis for lens_age_bar: oldest time_first in the list -> now."""
    starts = [dt for dt, _ in (_parse(_get(r, "time_first") or _get(r, "time_last")) for r in rows or []) if dt]
    if not starts:
        return None
    return min(starts).timestamp(), timezone.now().timestamp()


@register.filter
def lens_age_bar(row, axis):
    """Inline CSS placing a row's first -> last seen span on the shared axis."""
    if not axis:
        return ""
    first, _ = _parse(_get(row, "time_first"))
    last, _ = _parse(_get(row, "time_last"))
    first, last = first or last, last or first
    if not first:
        return ""
    t0, t1 = axis
    span = (t1 - t0) or 1
    left = min(max((first.timestamp() - t0) / span, 0), 1) * 100
    right = min(max((last.timestamp() - t0) / span, 0), 1) * 100
    return f"left:{left:.1f}%;width:{max(right - left, 0):.1f}%"


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


# Netdisco's OUI table describes locally-administered/randomized MAC ranges
# with entries like "randomized address [0-f][26ae]:xx:xx:xx" instead of a
# real vendor name — that regex-looking range spec means nothing to a user,
# so it's collapsed to a plain "Random" label.
_RANDOMIZED_VENDOR_RE = re.compile(r"^randomized address\b", re.IGNORECASE)


@register.filter
def lens_vendor(vendor):
    if vendor and _RANDOMIZED_VENDOR_RE.match(str(vendor)):
        return "Random"
    return vendor
