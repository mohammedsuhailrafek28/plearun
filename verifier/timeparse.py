"""Timestamp parsing.

Every timestamp becomes (seconds, precision_seconds, is_relative):
  seconds      float, Unix seconds (absolute) or seconds from file start (relative)
  precision    how coarse the source value is: 1 for HH:MM:SS, 60 for HH:MM
  is_relative  True only for WebVTT cue times, which have no wall clock at all
"""
import re
from datetime import datetime, timedelta, timezone, date as Date

ISO_OFFSET_RE = re.compile(r"[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$")
CLOCK_RE = re.compile(r"^~?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:([AaPp])\.?\s?[Mm]\.?)?$")
DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def tz_from_offset(text):
    """'-05:00' / '-0500' / 'Z' -> timezone."""
    if text == "Z":
        return timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])))


def find_tz_hint(strings):
    """Most common UTC offset seen in ISO strings, used for naive and epoch values."""
    counts = {}
    for s in strings:
        m = ISO_OFFSET_RE.search(s.strip())
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if not counts:
        return None
    # A local offset (-05:00) says more about the wall clock people wrote in than UTC "Z" does.
    local = {k: v for k, v in counts.items() if k not in ("Z", "+00:00", "+0000")}
    pool = local or counts
    return tz_from_offset(max(pool, key=pool.get))


class TimeContext:
    """What we need to interpret incomplete timestamps: local zone and the incident date."""

    def __init__(self, tz=None, base_date=None):
        self.tz = tz or timezone.utc
        self.base_date = base_date  # datetime.date or None


def parse_ts(value, ctx):
    """Return (seconds, precision, False) or None if value is not a timestamp we understand."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _epoch(value)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        return _epoch(float(s))
    m = CLOCK_RE.match(s)
    if m:
        if ctx.base_date is None:
            return None
        h, mi, sec = int(m.group(1)), int(m.group(2)), m.group(3)
        if m.group(4):
            if not 1 <= h <= 12:
                return None
            h = h % 12 + (12 if m.group(4).lower() == "p" else 0)
        if h > 23 or mi > 59:
            return None
        dt = datetime(ctx.base_date.year, ctx.base_date.month, ctx.base_date.day,
                      h, mi, int(sec or 0), tzinfo=ctx.tz)
        return dt.timestamp(), (1 if sec else 60), False
    iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
        for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%d-%b-%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ctx.tz)  # naive -> incident local zone (documented assumption)
    precision = 1 if re.search(r"\d{1,2}:\d{2}:\d{2}", s) else 60
    return dt.timestamp(), precision, False


def _epoch(v):
    v = float(v)
    if v > 1e14:      # microseconds
        v /= 1e6
    elif v > 1e11:    # milliseconds
        v /= 1e3
    elif v < 1e8:     # too small to be a plausible epoch
        return None
    return v, 1, False


def parse_vtt_time(s):
    """'00:00:03.500' or '00:03.500' -> seconds."""
    parts = s.strip().split(":")
    try:
        nums = [float(p.replace(",", ".")) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def fmt(seconds, ctx):
    return datetime.fromtimestamp(seconds, ctx.tz).strftime("%H:%M:%S")


def first_date(text):
    m = DATE_RE.search(text)
    return Date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
