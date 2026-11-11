#!/usr/bin/env python3
"""
gpxtag - geotag photos from one or more GPX track logs.

Reads the capture time of every photo, finds where you were at that moment
in the GPX track(s), and writes GPS tags into the photo. Photos taken
outside the track's time span (or in a long gap in the track) are left alone.

Time zones are taken from the photos themselves: modern cameras and phones
store the clock's UTC offset in each photo (EXIF OffsetTimeOriginal). GPX
tracks are always UTC, so normally nothing needs to be configured.

If the stored offset is wrong, or missing, use --tz to say what time zone the
camera clock was showing. It applies to every photo in the run. Give a place
(daylight saving is worked out from each photo's date) or a fixed offset.
If the camera clock itself was wrong (e.g. 90 s fast), use --shift.

Requires: Python 3.9+ and exiftool 10.28+ (https://exiftool.org) on PATH.
  Debian/Ubuntu: apt install libimage-exiftool-perl
  macOS:         brew install exiftool
Photos store their time zone only if they were written with EXIF 2.31 or
later (2016 onwards). Older photos need --tz.

Examples:
  gpxtag track.gpx                              # photos in the current folder
  gpxtag track.gpx ~/Photos/trip -r --dry-run   # preview, include subfolders
  gpxtag day1.gpx day2.gpx ~/Photos/trip        # several tracks at once
  gpxtag track.gpx --tz "los angeles"           # camera's stored offset is wrong/missing
  gpxtag track.gpx --tz tokyo                   # any city in the time zone database
  gpxtag track.gpx --tz -8                      # or a fixed UTC offset
  gpxtag track.gpx --shift -0:01:30             # camera clock was 90 s fast
"""

import argparse
import bisect
import difflib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import List, Optional, Tuple

# Load the time zone database module. Python versions before 3.9 do not
# have it. In that case, only fixed UTC offsets are available for --tz.
try:
    from zoneinfo import ZoneInfo, available_timezones
except ImportError:  # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------- #
# GPX parsing
# --------------------------------------------------------------------------- #

@dataclass
class TrackPoint:
    """One recorded position from a GPX track.

    Attributes:
        t: The time of the position, as a POSIX timestamp in UTC.
        lat: The latitude in decimal degrees.
        lon: The longitude in decimal degrees.
        ele: The elevation in meters. This value is None if the GPX
            file does not give an elevation.
        seg: The segment number. Do not interpolate between two points
            that have different segment numbers.
    """
    t: float            # POSIX timestamp (UTC)
    lat: float
    lon: float
    ele: Optional[float]
    seg: int            # Segment number. Do not interpolate across segments.


# This pattern matches an ISO 8601 date and time. The fraction of a second
# and the time zone are optional.
_ISO_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(\.\d+)?\s*(Z|z|[+-]\d\d:?\d\d)?$"
)


def parse_iso_time(s: str) -> float:
    """Change a GPX (ISO 8601) time text into a POSIX timestamp.

    If the text has no time zone, the function uses UTC.

    Args:
        s: The time text from the GPX file.

    Returns:
        The time as a POSIX timestamp in UTC.

    Raises:
        ValueError: The text is not a full ISO 8601 date and time.
    """
    m = _ISO_RE.match(s.strip())
    if not m:
        raise ValueError(f"unrecognised GPX time: {s!r}")
    base, frac, tz = m.groups()
    dt = datetime.fromisoformat(base.replace(" ", "T"))
    # Add the fraction of a second, if the text has one.
    if frac:
        dt += timedelta(seconds=float(frac))
    # Attach the time zone. If there is no time zone, use UTC.
    if not tz or tz in ("Z", "z"):
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
    return dt.timestamp()


def _local(tag: str) -> str:
    """Remove the XML namespace from a tag name.

    Args:
        tag: A tag name in ElementTree format, for example
            "{http://www.topografix.com/GPX/1/1}trkpt".

    Returns:
        The tag name without the namespace, for example "trkpt".
    """
    return tag.rsplit("}", 1)[-1]


# File extensions that the script reads as track logs.
TRACK_EXTS = (".gpx",)


def _try_time(text: Optional[str]) -> Optional[float]:
    """Change a GPX time text into a timestamp, if possible.

    A date without a time is not sufficiently precise. The function
    does not use it.

    Args:
        text: The time text from the GPX file, or None.

    Returns:
        The POSIX timestamp in UTC. The function returns None if the text
        is empty, has only a date, or is not a valid time.
    """
    if not text:
        return None
    try:
        return parse_iso_time(text)
    except ValueError:
        return None


def _read_xml(path: str, kind: str) -> ET.Element:
    """Read an XML file and return its root element.

    If the file is missing or is not valid XML, the script stops
    with an error message.

    Args:
        path: The path to the XML file.
        kind: The type of file, for the error message (for example "GPX").

    Returns:
        The root element of the XML file.
    """
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError) as e:
        sys.exit(f"error: cannot read {kind} file {path}: {e}")


def _read_gpx(path: str, segs) -> Tuple[List[TrackPoint], int]:
    """Read all track points from one GPX file.

    The function uses only points in track segments (trkseg) that have a
    full date and time. It counts route points (rtept), waypoints (wpt)
    and track points without a time, but it does not use them.

    Args:
        path: The path to the GPX file.
        segs: An iterator that gives a new segment number each time.
            Share one iterator between all files, so that each segment
            in the run has a different number.

    Returns:
        A tuple with two items:
        the list of track points that have a time, and
        the number of positions that have no usable time.
    """
    root = _read_xml(path, "GPX")
    pts: List[TrackPoint] = []
    untimed = 0
    # Look at all elements in the file. Process only the track segments.
    for seg in root.iter():
        if _local(seg.tag) != "trkseg":
            # Routes and waypoints are plans, not recordings.
            # Count them, but do not use them.
            if _local(seg.tag) in ("rtept", "wpt"):
                untimed += 1
            continue
        sid = next(segs)
        for pt in seg:
            if _local(pt.tag) != "trkpt":
                continue
            # Find the time and the elevation of this point.
            t = ele = None
            for child in pt:
                name = _local(child.tag)
                if name == "time":
                    t = _try_time(child.text)
                elif name == "ele" and child.text:
                    try:
                        ele = float(child.text)
                    except ValueError:
                        pass
            # Ignore a point that has no valid latitude or longitude.
            try:
                lat, lon = float(pt.get("lat")), float(pt.get("lon"))
            except (TypeError, ValueError):
                continue
            # Count a point that has no time, but do not use it.
            if t is None:
                untimed += 1
                continue
            pts.append(TrackPoint(t, lat, lon, ele, sid))
    return pts, untimed


def load_tracks(paths: List[str], quiet: bool = False) -> List[TrackPoint]:
    """Read all GPX files and combine their track points.

    The function shows a status line for each file, unless quiet is
    True. If no file has track points with a time, the script stops
    with exit code 2.

    Args:
        paths: The paths to the GPX files.
        quiet: If True, do not show the status lines.

    Returns:
        All track points from all files, in time sequence.
    """
    segs = itertools.count(1)
    pts: List[TrackPoint] = []
    for path in paths:
        got, untimed = _read_gpx(path, segs)
        name = os.path.basename(path)
        # Tell the user what the script found in this file.
        if quiet:
            pass
        elif got:
            note = f" ({untimed} more without times were ignored)" if untimed else ""
            print(f"Read {name}: {len(got)} timed points{note}")
        elif untimed:
            print(f"Skipping {name}: it has {untimed} position{'s' if untimed != 1 else ''} "
                  "but no timestamps (date and time), so it can't show when you were where. "
                  "It's probably a drawn or planned route, not a recorded track.")
        else:
            print(f"Skipping {name}: no track points found in it.")
        pts += got
    # Stop if no file has a usable track point.
    if not pts:
        print("\nNothing to do: none of the GPX files have timestamps. Export a "
              "*recorded* track from your GPS app and try again.")
        sys.exit(2)
    pts.sort(key=lambda p: p.t)
    return pts


# --------------------------------------------------------------------------- #
# Position lookup
# --------------------------------------------------------------------------- #

@dataclass
class Fix:
    """The position that the script calculates for one photo.

    Attributes:
        lat: The latitude in decimal degrees.
        lon: The longitude in decimal degrees.
        ele: The elevation in meters, or None if it is not known.
        how: The method that gave the position. "exact" means that a track
            point has the same time as the photo. "interp" means that the
            position is between two track points. "nearest" means that the
            script used the nearest track point.
        dt: The time in seconds between the photo and the nearest
            real track point.
    """
    lat: float
    lon: float
    ele: Optional[float]
    how: str            # "exact", "interp" or "nearest"
    dt: float           # Seconds to the nearest real track point


class Track:
    """A track log that finds the position at a given time.

    Attributes:
        pts: All track points, in time sequence.
        times: The time of each track point. This list has the same
            sequence as pts. The binary search uses it.
        max_gap: The maximum time in seconds between two points.
            If the gap is larger, the script does not interpolate.
        tolerance: The maximum time in seconds to the nearest point.
            If the time is larger, the script does not use that point.
    """

    def __init__(self, pts: List[TrackPoint], max_gap: float, tolerance: float):
        """Make a track from a list of points.

        Args:
            pts: The track points. They must be in time sequence.
            max_gap: The maximum time in seconds to interpolate across.
            tolerance: The maximum time in seconds to the nearest point.
        """
        self.pts = pts
        self.times = [p.t for p in pts]
        self.max_gap = max_gap
        self.tolerance = tolerance

    @property
    def start(self) -> float:
        """The time of the first track point, as a POSIX timestamp."""
        return self.times[0]

    @property
    def end(self) -> float:
        """The time of the last track point, as a POSIX timestamp."""
        return self.times[-1]

    def locate(self, t: float) -> Tuple[Optional[Fix], str]:
        """Find the position at a given time.

        The function tries these methods in this sequence:

        1. Use a track point that has the same time.
        2. Interpolate between the two points before and after the time.
           The two points must be in the same segment. The gap between
           them must not be more than max_gap.
        3. Use the nearest track point, if it is within the tolerance.

        Args:
            t: The time of the photo, as a POSIX timestamp in UTC.

        Returns:
            A tuple with two items: the position and a reason.
            If the function finds a position, the reason is empty.
            If it does not find a position, the position is None and
            the reason tells why.
        """
        n = len(self.pts)
        i = bisect.bisect_left(self.times, t)

        # Method 1: a track point has the same time as the photo.
        if i < n and self.times[i] == t:
            p = self.pts[i]
            return Fix(p.lat, p.lon, p.ele, "exact", 0.0), ""

        # Find the points immediately before (a) and after (b) the time.
        a = self.pts[i - 1] if i > 0 else None
        b = self.pts[i] if i < n else None

        # Method 2: interpolate between a and b. The two points must be
        # in the same segment, and the gap between them must be small.
        if a and b and a.seg == b.seg and (b.t - a.t) <= self.max_gap:
            f = (t - a.t) / (b.t - a.t)
            dlon = b.lon - a.lon
            # Correct the longitude difference if the track crosses
            # the 180 degree meridian.
            if dlon > 180:
                dlon -= 360
            elif dlon < -180:
                dlon += 360
            lon = a.lon + f * dlon
            lon = (lon + 180) % 360 - 180
            # Interpolate the elevation if both points have one.
            # If only one point has an elevation, use that value.
            ele = None
            if a.ele is not None and b.ele is not None:
                ele = a.ele + f * (b.ele - a.ele)
            else:
                ele = a.ele if a.ele is not None else b.ele
            return Fix(a.lat + f * (b.lat - a.lat), lon, ele, "interp",
                       min(t - a.t, b.t - t)), ""

        # Method 3: use the nearest point if it is within the tolerance.
        cands = [p for p in (a, b) if p is not None]
        near = min(cands, key=lambda p: abs(p.t - t))
        d = abs(near.t - t)
        if d <= self.tolerance:
            return Fix(near.lat, near.lon, near.ele, "nearest", d), ""

        # No method found a position. Tell why.
        if t < self.start:
            return None, f"before track start by {fmt_dur(self.start - t)}"
        if t > self.end:
            return None, f"after track end by {fmt_dur(t - self.end)}"
        return None, f"in track gap ({fmt_dur(d)} from nearest point)"


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

# This pattern matches an EXIF date and time ("YYYY:MM:DD HH:MM:SS").
# The fraction of a second and the time zone are optional.
_EXIF_DT_RE = re.compile(
    r"^(\d{4}):(\d\d):(\d\d)[ T](\d\d):(\d\d):(\d\d)(\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?"
)


def fmt_dur(sec: float) -> str:
    """Change a duration into a short text for a person to read.

    The sign of the duration is ignored.

    Args:
        sec: The duration in seconds.

    Returns:
        A short text, for example "2d3h05m", "1h30m", "4m09s" or "12s".
    """
    sec = int(round(abs(sec)))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    # Show only the largest units that are necessary.
    if d:
        return f"{d}d{h}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def parse_shift(spec: str) -> float:
    """Change the --shift value into a number of seconds.

    These formats are valid:
    "+1:00" (H:MM), "-0:01:30" (H:MM:SS) and "90" (seconds).

    Args:
        spec: The text that the user gave for --shift.

    Returns:
        The shift in seconds. A negative value moves the photo times
        to an earlier time.

    Raises:
        argparse.ArgumentTypeError: The text is not in a valid format.
    """
    s = spec.strip()
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    # Format with colons: H:MM or H:MM:SS.
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        if len(parts) == 2:        # H:MM
            parts = parts + [0.0]
        if len(parts) != 3:
            raise argparse.ArgumentTypeError(f"bad shift {spec!r}")
        h, m, sec = parts
        return sign * (h * 3600 + m * 60 + sec)
    # Format without colons: a number of seconds.
    try:
        return sign * float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad shift {spec!r}")


# This pattern matches a UTC offset, for example "+09:00" or "-0800".
_OFFSET_RE = re.compile(r"^([+-])(\d\d):?(\d\d)$")
# The EXIF tags that can contain the UTC offset, in sequence of priority.
OFFSET_TAGS = ("OffsetTimeOriginal", "OffsetTimeDigitized", "OffsetTime")
# The date tags that the script reads, and the offset tag that belongs to
# each date tag.
DATE_OFFSET_TAGS = {
    "SubSecDateTimeOriginal": "OffsetTimeOriginal",
    "DateTimeOriginal": "OffsetTimeOriginal",
    "CreateDate": "OffsetTimeDigitized",
}
# The range of real UTC offsets: UTC-12:00 to UTC+14:00.
MIN_OFFSET = -timedelta(hours=12)
MAX_OFFSET = timedelta(hours=14)


def parse_offset(text: str) -> timedelta:
    """Change an offset text into a time difference.

    Args:
        text: An offset in the format "+HH:MM" or "-HHMM".

    Returns:
        The offset as a time difference from UTC.

    Raises:
        ValueError: The text is not a valid offset.
    """
    m = _OFFSET_RE.match(text)
    if not m:
        raise ValueError(f"bad UTC offset: {text!r}")
    sign = 1 if m.group(1) == "+" else -1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))


def photo_offset(rec: dict, date_tag: Optional[str] = None) -> Optional[str]:
    """Find the UTC offset that the camera stored in the photo.

    Each date tag has its own offset tag. If the caller gives the date
    tag, the function first uses the offset tag that belongs to it.
    Then it uses the other offset tags.

    Args:
        rec: The exiftool data for one photo.
        date_tag: The date tag that gave the capture time, or None.

    Returns:
        The offset in the format "+HH:MM". The function returns None
        if the photo has no valid offset.
    """
    keys = list(OFFSET_TAGS)
    own = DATE_OFFSET_TAGS.get(date_tag or "")
    if own:
        keys.remove(own)
        keys.insert(0, own)
    # Use the first tag that has a valid offset.
    for key in keys:
        v = rec.get(key)
        if isinstance(v, str):
            v = v.strip()
            if v in ("Z", "+00:00", "-00:00"):
                return "+00:00"
            m = _OFFSET_RE.match(v)
            if m:
                return f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    return None


def fmt_offset(off: timedelta) -> str:
    """Change a UTC offset into text.

    Args:
        off: The UTC offset.

    Returns:
        The offset in the format "+HH:MM" or "-HH:MM".
    """
    mins = int(off.total_seconds() // 60)
    sign = "-" if mins < 0 else "+"
    h, m = divmod(abs(mins), 60)
    return f"{sign}{h:02d}:{m:02d}"


def _same_rules(ids: List[str]) -> bool:
    """Find out if some time zones always have the same UTC offset.

    The function compares the offsets each day from 1970 to 2040.

    Args:
        ids: Time zone IDs, for example "America/Buenos_Aires".

    Returns:
        True if all the time zones have the same offset on each day.
    """
    zones = [ZoneInfo(z) for z in ids]
    t = datetime(1970, 1, 1, tzinfo=timezone.utc)
    while t.year < 2040:
        if len({t.astimezone(z).utcoffset() for z in zones}) > 1:
            return False
        t += timedelta(days=1)
    return True


def resolve_tz(spec: str) -> Tuple[tzinfo, str]:
    """Change the --tz value into a time zone.

    These values are valid:

    - Fixed offsets, for example -8, +9, -08:00, +0530 or UTC-7.
    - Places from the time zone database of the system, for example
      "los angeles", tokyo, America/Denver or Europe/London.
      The case of letters and the spaces are not important.

    If the value is not valid, the script stops with an error message.
    If possible, the message gives names of places that are similar.

    Args:
        spec: The text that the user gave for --tz.

    Returns:
        A tuple with two items: the time zone and a label for it.
    """
    raw = spec.strip()
    low = raw.lower()
    # Names for UTC.
    if low in ("utc", "gmt", "z", "zulu"):
        return timezone.utc, "UTC"
    # Fixed offset, with an optional "UTC" or "GMT" before it.
    m = re.fullmatch(r"(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d\d))?", low)
    if m:
        h, mm = int(m.group(2)), int(m.group(3) or 0)
        off = (1 if m.group(1) == "+" else -1) * timedelta(hours=h, minutes=mm)
        if mm >= 60 or not MIN_OFFSET <= off <= MAX_OFFSET:
            sys.exit(f"error: {raw!r} isn't a real UTC offset (they go from -12:00 to +14:00)")
        return timezone(off), f"UTC{fmt_offset(off)}"

    # A place name. Make sure that the time zone database is available.
    if ZoneInfo is None:
        sys.exit("error: place names need Python 3.9+; use an offset like --tz -8")
    try:
        ids = available_timezones()
    except Exception:
        ids = set()
    if not ids:
        sys.exit("error: this system has no time zone database (pip install tzdata), "
                 "or use an offset like --tz -8")

    def norm(x: str) -> str:
        """Make a place name simple, so that names are easy to compare.

        The function changes all letters to lower case. It changes
        spaces and punctuation into one space.

        Args:
            x: A place name or a time zone ID.

        Returns:
            The simple name, for example "america los angeles".
        """
        return re.sub(r"[\s_\-./,']+", " ", x.lower()).strip()

    n = norm(raw)
    # Use only real "Area/City" zones. Ignore old alias zones.
    real = [z for z in ids if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/"))]
    # First, compare with the full zone ID. Then, compare with the city part only.
    hits = [z for z in real if norm(z) == n] or [z for z in real if norm(z.split("/")[-1]) == n]
    # Some places have an old alias ID and a current ID, for example
    # America/Buenos_Aires and America/Argentina/Buenos_Aires. If all the
    # hits have the same rules, they are one place. Use the longest ID,
    # which is usually the current one.
    if len(hits) > 1 and _same_rules(hits):
        hits = [sorted(hits, key=lambda z: (-z.count("/"), z))[0]]
    if len(hits) == 1:
        return ZoneInfo(hits[0]), hits[0]
    if len(hits) > 1:
        sys.exit(f"error: --tz {raw!r} matches several places: {', '.join(sorted(hits))}")

    # No match. Find similar city names and stop with an error message.
    cities = {norm(z.split("/")[-1]): z for z in real}
    close = difflib.get_close_matches(n, list(cities), n=5, cutoff=0.6)
    msg = f"error: don't know the place {raw!r}."
    if close:
        msg += " Did you mean: " + ", ".join(f'"{c}"' if " " in c else c for c in close) + "?"
    msg += ("\nUse a big city in the same time zone (e.g. --tz \"los angeles\", --tz tokyo, "
            "--tz london) or an offset (e.g. --tz -8).")
    sys.exit(msg)


def photo_timestamp(rec: dict, tz_override: Optional[tzinfo] = None
                    ) -> Tuple[List[Tuple[float, str]], str]:
    """Find the capture time of a photo in UTC.

    The function uses the time zone in this sequence of priority:

    1. The --tz time zone, if the user gave one.
    2. The UTC offset that the camera stored in the photo. The function
       first uses the offset tag that belongs to the date tag.
    3. A time zone that is part of the date and time text.

    With a --tz place, a clock time can be ambiguous. When daylight saving
    time ends, the clock shows the same hour two times. When it starts, the
    clock skips an hour. In these two cases, the function gives two
    possible times.

    Args:
        rec: The exiftool data for one photo.
        tz_override: The time zone from --tz, or None.

    Returns:
        A tuple with two items: a list of possible times, and a reason.
        Each possible time is a tuple of the POSIX timestamp in UTC and
        the offset that the function used. Usually the list has one item.
        If the function does not find the time, the list is empty and the
        reason tells why.
    """
    # Find the first tag that has a valid capture time.
    raw = date_tag = None
    for key in DATE_OFFSET_TAGS:
        v = rec.get(key)
        if isinstance(v, str) and not v.startswith("0000"):
            raw, date_tag = v, key
            break
    m = _EXIF_DT_RE.match(raw) if raw else None
    if not m:
        return [], "no capture time in the photo"
    y, mo, d, hh, mi, ss, frac, inline_tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss))
    if frac:
        dt += timedelta(seconds=float(frac))

    # Priority 1: the --tz time zone.
    if tz_override is not None:
        # A place time zone calculates daylight saving time from the date
        # of this photo. Fold 0 and fold 1 are the two possible readings
        # of an ambiguous clock time. They are equal for other times.
        out = []
        for fold in (0, 1):
            aware = dt.replace(tzinfo=tz_override, fold=fold)
            cand = (aware.timestamp(), fmt_offset(aware.utcoffset()))
            if cand not in out:
                out.append(cand)
        return out, ""

    # Priority 2 and 3: the stored offset, or the time zone in the text.
    off = photo_offset(rec, date_tag)
    if off is None and inline_tz:
        off = "+00:00" if inline_tz == "Z" else inline_tz
    if off is None:
        return [], "no time zone stored in the photo (use --tz)"
    off = fmt_offset(parse_offset(off))
    tz = timezone(parse_offset(off))
    return [(dt.replace(tzinfo=tz).timestamp(), off)], ""


def camera_reading(rec: dict) -> Optional[str]:
    """Find the capture time exactly as the camera clock showed it.

    The function does not apply a time zone or a shift.
    The script uses this text only to show to the user.

    Args:
        rec: The exiftool data for one photo.

    Returns:
        The time in the format "YYYY-MM-DD HH:MM:SS". The function returns
        None if the photo has no valid capture time.
    """
    for key in ("DateTimeOriginal", "CreateDate"):
        v = rec.get(key)
        if isinstance(v, str):
            m = _EXIF_DT_RE.match(v)
            if m and not v.startswith("0000"):
                y, mo, d, hh, mi, ss = m.groups()[:6]
                return f"{y}-{mo}-{d} {hh}:{mi}:{ss}"
    return None


def utc_str(ts: float) -> str:
    """Change a timestamp into UTC text.

    Args:
        ts: A POSIX timestamp.

    Returns:
        The time in the format "YYYY-MM-DD HH:MM:SSZ".
    """
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# --------------------------------------------------------------------------- #
# exiftool I/O
# --------------------------------------------------------------------------- #

# The tags that the script reads from each photo with exiftool.
READ_TAGS = [
    "-SubSecDateTimeOriginal", "-DateTimeOriginal", "-CreateDate",
    "-OffsetTimeOriginal", "-OffsetTimeDigitized", "-OffsetTime", "-GPSLatitude", "-GPSLongitude", "-MIMEType",
]

# The minimum exiftool version. Version 10.28 added the EXIF 2.31 time zone
# tags (OffsetTimeOriginal and others) that the script reads.
MIN_EXIFTOOL = (10, 28)


def exiftool_version() -> Optional[Tuple[int, int]]:
    """Find the version of exiftool.

    Returns:
        The version as a tuple (major, minor), for example (13, 55).
        The function returns None if it cannot read the version.
    """
    try:
        res = subprocess.run(["exiftool", "-ver"], capture_output=True, text=True)
    except OSError:
        return None
    m = re.match(r"^(\d+)\.(\d+)", res.stdout.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def read_photos(paths: List[str], recursive: bool, exts: List[str]) -> List[dict]:
    """Read the time and GPS tags of the photos with exiftool.

    If the user gives no extensions, the function keeps only still
    images. If exiftool fails, the script stops with an error message.

    Args:
        paths: The photo files and folders to read.
        recursive: If True, also read the subfolders.
        exts: The file extensions to read. If the list is empty,
            exiftool reads all file types.

    Returns:
        A list with one dictionary of exiftool data for each photo.
        The "SourceFile" key gives the path of the photo.
    """
    # Make the exiftool command. "-n" gives numbers, not formatted text.
    cmd = ["exiftool", "-json", "-n", "-q", "-q", "-charset", "filename=utf8"] + READ_TAGS
    if recursive:
        cmd.append("-r")
    for e in exts:
        cmd += ["-ext", e.lstrip(".")]
    cmd += paths
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    # No output means that exiftool found no files, or that it failed.
    if not res.stdout.strip():
        if res.returncode not in (0, 1):
            sys.exit(f"error: exiftool failed:\n{res.stderr}")
        return []
    try:
        recs = json.loads(res.stdout)
    except json.JSONDecodeError:
        sys.exit(f"error: could not parse exiftool output:\n{res.stderr}")
    if exts:
        return recs
    # The user gave no extensions. Keep only still images.
    return [r for r in recs if str(r.get("MIMEType", "")).startswith("image/")]


def write_gps(jobs: List[Tuple[str, Fix, float]], backup: bool, keep_mtime: bool,
              clear: bool = False) -> Tuple[int, List[str]]:
    """Write the GPS tags into the photos with exiftool.

    The function writes all commands into one argument file. Thus,
    exiftool starts only one time for all photos.

    Args:
        jobs: A list of tuples. Each tuple has the photo path, the
            position, and the capture time as a POSIX timestamp in UTC.
        backup: If True, exiftool keeps a copy of each original file
            (FILE_original). If False, exiftool changes the file in place.
        keep_mtime: If True, exiftool keeps the file modification time.
        clear: If True, exiftool first deletes all EXIF GPS tags of the
            photo. Use this with --overwrite, so that no old GPS tag (for
            example an old altitude) stays with the new position.

    Returns:
        A tuple with two items: the number of files that exiftool
        updated, and a list of error lines from exiftool.
    """
    # Make one block of exiftool arguments for each photo.
    lines: List[str] = []
    for path, fix, ts in jobs:
        utc = datetime.fromtimestamp(ts, timezone.utc)
        # Delete the old GPS tags before the new tags are written.
        # exiftool applies the assignments in sequence.
        if clear:
            lines.append("-gps:all=")
        lines += [
            f"-GPSLatitude={abs(fix.lat):.7f}",
            f"-GPSLatitudeRef={'N' if fix.lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(fix.lon):.7f}",
            f"-GPSLongitudeRef={'E' if fix.lon >= 0 else 'W'}",
            f"-GPSDateStamp={utc:%Y:%m:%d}",
            f"-GPSTimeStamp={utc:%H:%M:%S}",
            "-GPSMapDatum=WGS-84",
        ]
        # Write the altitude only if the position has an elevation.
        # Reference 0 is above sea level. Reference 1 is below sea level.
        if fix.ele is not None:
            lines += [
                f"-GPSAltitude={abs(fix.ele):.1f}",
                f"-GPSAltitudeRef#={0 if fix.ele >= 0 else 1}",
            ]
        if not backup:
            lines.append("-overwrite_original")
        if keep_mtime:
            lines.append("-P")
        # "-execute" ends the commands for this photo.
        lines += [path, "-execute"]

    # Write the arguments to a temporary file and run exiftool one time.
    # Always delete the temporary file after exiftool stops.
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        argfile = f.name
    try:
        res = subprocess.run(
            ["exiftool", "-charset", "filename=utf8", "-@", argfile],
            capture_output=True, text=True, encoding="utf-8",
        )
    finally:
        os.unlink(argfile)

    # Add the "N image files updated" counts from the exiftool output.
    # Keep the error lines, but ignore warnings.
    updated = sum(int(m) for m in re.findall(r"(\d+) image files? updated", res.stdout))
    errors = [l for l in res.stderr.splitlines() if l.strip() and not l.startswith("Warning")]
    return updated, errors


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def fmt_shift(sec: float) -> str:
    """Change a shift in seconds into the --shift text format.

    Args:
        sec: The shift in seconds.

    Returns:
        The shift in the format "+H:MM" or "-H:MM:SS". The function adds
        the seconds only if they are not zero.
    """
    sign = "-" if sec < 0 else "+"
    sec = int(round(abs(sec)))
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}" + (f":{s_:02d}" if s_ else "")


def tz_hint(track: Track, placed: List[Tuple[float, str]],
            missed: List[Tuple[float, str]], shift: float) -> Optional[str]:
    """Find a --tz or --shift value that places more photos.

    A wrong time zone moves all photos by full or half hours. The function
    tries each offset from -14 h to +14 h in 30-minute steps on the
    skipped photos. The best offset must place at least half of them.
    Then the function applies the suggested option to all photos, also
    the photos that have a position now. It gives the hint only if the
    option places more photos in total. If not, the skipped photos
    probably need their own run, and the hint says this.

    Note: this finds only photos that are outside the track. On a long
    track of many days, a wrong time zone can put a photo inside the
    track at the wrong position. Examine the --dry-run output.

    Args:
        track: The track.
        placed: The (timestamp, offset used) of the photos that have a
            position now. The timestamps include the --shift value.
        missed: The (timestamp, offset used) of the photos outside the
            track or in a gap.
        shift: The --shift value in seconds.

    Returns:
        The hint text, or None if there is no useful hint.
    """
    def count(times) -> int:
        """Count the times that have a position on the track."""
        return sum(1 for t in times if track.locate(t)[0] is not None)

    # Find the offset that places the most skipped photos.
    # If two offsets place the same number, use the smaller offset.
    best, best_n = 0.0, 0
    for k in range(-28, 29):              # -14 h to +14 h in 30-minute steps
        if k == 0:
            continue
        n = count(t + k * 1800 for t, _ in missed)
        if n > best_n or (n == best_n and abs(k) < abs(best / 1800)):
            best, best_n = k * 1800.0, n
    # The offset must place at least half of the skipped photos.
    if best_n == 0 or best_n < (len(missed) + 1) // 2:
        return None

    # Make the suggestion. If all skipped photos have the same offset,
    # suggest a --tz value. Else, suggest a --shift value.
    # Then calculate the new times of all photos with the suggestion.
    everyone = placed + missed
    used = {o for _, o in missed}
    new = None
    if len(used) == 1:
        o = next(iter(used))
        new_off = parse_offset(o) - timedelta(seconds=best)
        if MIN_OFFSET <= new_off <= MAX_OFFSET:
            new = fmt_offset(new_off)
    if new is not None:
        # With --tz, each photo gets the new offset instead of its own one.
        moved = [t + (parse_offset(off) - new_off).total_seconds() for t, off in everyone]
        short = new[:3].replace("+0", "+").replace("-0", "-") if new.endswith(":00") else new
        advice = (f"if their time zone were UTC{new}, not UTC{o}. Try --tz {short} (or the "
                  f"place's name, e.g. --tz \"los angeles\")")
        own_run = f"--tz {short}"
    else:
        moved = [t + best for t, _ in everyone]
        advice = f"with --shift {fmt_shift(shift + best)}"
        own_run = f"--shift {fmt_shift(shift + best)}"

    # Compare the total number of photos with a position.
    total = count(moved)
    lost = len(placed) - count(moved[:len(placed)])
    if total > len(placed):
        extra = f" ({lost} photo(s) that match now would no longer match)" if lost else ""
        return f"Hint: {best_n} of {len(missed)} skipped photos would match {advice}.{extra}"
    return (f"Hint: {best_n} of {len(missed)} skipped photos would match if their times "
            f"moved by {fmt_shift(best)}, but doing that for every photo would lose more "
            f"matches than it gains. If those photos came from another camera or have a "
            f"wrong stored time zone, tag them in a separate run with {own_run}.")


def main() -> int:
    """Run the gpxtag command.

    The function does these steps:

    1. Read the command line arguments.
    2. Read the GPX tracks and the photo tags.
    3. Find a position for each photo.
    4. Write the GPS tags (unless --dry-run is set).
    5. Show a summary and hints.

    Returns:
        The exit code. 0 means success. 1 means that exiftool gave
        errors or did not update all photos.
    """
    # Define the command line arguments. Use the second part of the module
    # docstring as the epilog. With "python -OO" there is no docstring.
    doc = __doc__ or ""
    epilog = "Time zones are" + doc.split("Time zones are", 1)[1] if "Time zones are" in doc else None
    ap = argparse.ArgumentParser(
        prog="gpxtag",
        description="Geotag photos using the time and position data in GPX track logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    ap.add_argument("inputs", nargs="*", metavar="PATH",
                    help="one or more .gpx files, then photo folders/files "
                         "(default: the current folder)")
    ap.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    ap.add_argument("--tz", metavar="PLACE|OFFSET",
                    help="time zone the camera clock showed, used for every photo instead "
                         "of the offset stored in it. A place (\"los angeles\", tokyo, "
                         "America/Denver; DST worked out per photo) or an offset (-8, +09:00)")
    ap.add_argument("--shift", type=parse_shift, default=0.0, metavar="[-]H:MM[:SS]",
                    help="amount to add to the photos' times, to fix a camera clock that "
                         "was simply wrong (e.g. --shift -0:01:30 if it ran 90 s fast)")
    ap.add_argument("--max-gap", type=float, default=300, metavar="SEC",
                    help="max time between two track points to interpolate across (default 300)")
    ap.add_argument("--tolerance", type=float, default=60, metavar="SEC",
                    help="snap to nearest point if within this many seconds, also allows "
                         "photos slightly before/after the track (default 60)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace GPS data on photos that already have it (default: skip)")
    ap.add_argument("--ext", action="append", default=[], metavar="EXT",
                    help="only process this extension (repeatable, e.g. --ext jpg --ext arw)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would be done, write nothing")
    ap.add_argument("--backup", action="store_true",
                    help="keep exiftool's FILE_original backups (default: overwrite in place)")
    ap.add_argument("--no-preserve-mtime", action="store_true",
                    help="let file modification times change (default: preserved)")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    # Join "--shift" and "--tz" to their values with "=". This lets
    # negative values such as "--shift -0:01:30" and "--tz -08:00" work.
    # Without this step, argparse reads a negative value as an option.
    argv = sys.argv[1:]
    i = 0
    while i < len(argv) - 1:
        if argv[i] in ("--shift", "--tz") and argv[i + 1].startswith("-"):
            argv[i:i + 2] = [f"{argv[i]}={argv[i + 1]}"]
        i += 1
    args = ap.parse_intermixed_args(argv)

    # Make sure that the user gave a GPX file and that exiftool is available.
    if not args.inputs:
        ap.error("give a .gpx file, optionally followed by photo folders/files "
                 "(default: current folder)")
    if not shutil.which("exiftool"):
        sys.exit("error: exiftool not found on PATH (apt install libimage-exiftool-perl / brew install exiftool)")
    # Make sure that exiftool is not too old. If the version is not
    # known, continue.
    ver = exiftool_version()
    if ver is not None and ver < MIN_EXIFTOOL:
        sys.exit(f"error: exiftool {ver[0]}.{ver[1]:02d} is too old; gpxtag needs "
                 f"{MIN_EXIFTOOL[0]}.{MIN_EXIFTOOL[1]:02d} or newer (it reads the EXIF 2.31 "
                 "time zone tags). Update it: brew upgrade exiftool / https://exiftool.org")

    # Divide the inputs into GPX files and photo paths.
    gpx_files = [p for p in args.inputs if p.lower().endswith(TRACK_EXTS) and os.path.isfile(p)]
    targets = [p for p in args.inputs if p not in gpx_files]
    if not gpx_files:
        ap.error("no .gpx file given")
    if not targets:
        targets = ["."]   # The user gave no photo path. Use the current folder.
    for t in targets:
        if not os.path.exists(t):
            ap.error(f"not found: {t}")

    # Read the time zone, the tracks and the photos.
    tz_override, tz_label = resolve_tz(args.tz) if args.tz else (None, "")
    pts = load_tracks(gpx_files, args.quiet)
    track = Track(pts, args.max_gap, args.tolerance)
    recs = read_photos(targets, args.recursive, args.ext)

    # Show the track times in the same clock time as the photos.
    # This makes the track and the photos easy to compare.
    # Use the --tz time zone, or else the offset that most photos have.
    if tz_override is not None:
        shown_tz = tz_override
        shown_off = fmt_offset(datetime.fromtimestamp(track.start, tz_override).utcoffset())
    else:
        offsets = Counter(o for o in (photo_offset(r) for r in recs) if o)
        shown_off = offsets.most_common(1)[0][0] if offsets else "+00:00"
        shown_tz = timezone(parse_offset(shown_off))

    def clock(ts: float) -> str:
        """Change a timestamp into clock time in the time zone of the photos.

        Args:
            ts: A POSIX timestamp.

        Returns:
            The time in the format "YYYY-MM-DD HH:MM:SS".
        """
        return datetime.fromtimestamp(ts, shown_tz).strftime("%Y-%m-%d %H:%M:%S")

    # Show the time zone, the track span and the number of photos.
    if not args.quiet:
        if tz_override is not None:
            print(f"Time zone: {tz_label} for all photos (from --tz, replacing their stored offsets)")
        print(f"Track:  {clock(track.start)} -> {clock(track.end)} (UTC{shown_off}, "
              f"{len(pts)} points, {fmt_dur(track.end - track.start)})")
        where = os.path.abspath(targets[0]) if targets == ["."] else ", ".join(targets)
        print(f"Photos: {len(recs)} in {where}" + (" (and subfolders)" if args.recursive else "")
              + (f", shifted {fmt_shift(args.shift)}" if args.shift else "") + "\n")

    # Find a position for each photo. Record a result line for each photo.
    # Put the photos that can get a position into the jobs list.
    jobs: List[Tuple[str, Fix, float]] = []
    # The (timestamp, offset used) of the photos that got a position, and
    # of the photos outside the track. The time zone hint uses them.
    placed: List[Tuple[float, str]] = []
    missed: List[Tuple[float, str]] = []
    counts = {"no_time": 0, "no_tz": 0, "has_gps": 0, "outside": 0, "gap": 0, "ambiguous": 0}
    rows = []
    for rec in sorted(recs, key=lambda r: r.get("SourceFile", "")):
        path = rec["SourceFile"]
        name = os.path.relpath(path)
        shown = camera_reading(rec) or "?"
        # Skip a photo that already has GPS data, unless --overwrite is set.
        if rec.get("GPSLatitude") is not None and not args.overwrite:
            counts["has_gps"] += 1
            rows.append(f"  skip  {name}: already has GPS (use --overwrite)")
            continue
        # Skip a photo that has no capture time or no time zone.
        cands, why = photo_timestamp(rec, tz_override)
        if not cands:
            counts["no_tz" if "zone" in why else "no_time"] += 1
            rows.append(f"  skip  {name} [{shown}]: {why}")
            continue
        # Apply the --shift value, then find the position for each
        # possible time. Usually there is only one possible time.
        tried = []
        for ts, off in cands:
            fix, why = track.locate(ts + args.shift)
            tried.append((ts + args.shift, off, fix, why))
        hits = [x for x in tried if x[2] is not None]
        # Skip a photo that is outside the track or in a gap.
        # Keep its time for the time zone hint.
        if not hits:
            ts, off, _, why = tried[0]
            counts["gap" if "gap" in why else "outside"] += 1
            missed.append((ts, off))
            rows.append(f"  skip  {name} [{shown} UTC{off}]: {why}")
            continue
        ts, off, fix, _ = hits[0]
        # The clock time was ambiguous (a daylight saving time change).
        # Tell the user which reading the script used.
        note = ""
        if len(hits) > 1:
            counts["ambiguous"] += 1
            note = (f"; ambiguous clock time, also fits UTC{hits[1][1]}: "
                    "check this one")
        elif len(tried) > 1:
            note = f"; ambiguous clock time, only the UTC{off} reading fits the track"
        jobs.append((path, fix, ts))
        placed.append((ts, off))
        how = fix.how if fix.how != "nearest" else f"nearest, {fmt_dur(fix.dt)} off"
        ele = f" {fix.ele:.0f}m" if fix.ele is not None else ""
        rows.append(f"  {'would' if args.dry_run else 'tag  '} {name} [{shown} UTC{off}] -> "
                    f"{fix.lat:.6f},{fix.lon:.6f}{ele} ({how}{note})")

    if not args.quiet:
        print("\n".join(rows) + ("\n" if rows else ""))

    # Write the GPS tags, unless --dry-run is set. With --overwrite,
    # delete the old GPS tags first.
    updated, errors = 0, []
    if jobs and not args.dry_run:
        updated, errors = write_gps(jobs, args.backup, not args.no_preserve_mtime,
                                    clear=args.overwrite)

    # Show the summary line and the exiftool errors.
    verb = "would tag" if args.dry_run else "tagged"
    parts = [f"{len(jobs) if args.dry_run else updated} {verb}",
             f"{counts['outside']} outside track", f"{counts['gap']} in gaps",
             f"{counts['has_gps']} already had GPS"]
    if counts["no_tz"]:
        parts.append(f"{counts['no_tz']} without a stored time zone")
    if counts["no_time"]:
        parts.append(f"{counts['no_time']} without a capture time")
    print("Summary: " + ", ".join(parts))
    for e in errors:
        print(f"  exiftool: {e}", file=sys.stderr)

    if not args.quiet:
        if missed:
            hint = tz_hint(track, placed, missed, args.shift)
            if hint:
                print("\n" + hint)
        # Tell the user how to fix photos that have no time zone.
        if counts["no_tz"]:
            print("\nNote: some photos don't record a time zone (older cameras, or files "
                  "edited by software that strips it). Tell gpxtag with --tz, e.g. "
                  "--tz \"los angeles\" or --tz -8.")
        # Tell the user about photos in an ambiguous hour.
        if counts["ambiguous"]:
            print(f"\nNote: {counts['ambiguous']} photo(s) have a clock time that a daylight "
                  "saving change makes ambiguous, and both readings fit the track. gpxtag "
                  "used the first one; check them in the list above.")

    return 1 if errors or (not args.dry_run and updated < len(jobs)) else 0


# Run the command. Stop quietly if the output pipe closes (for example,
# output sent to "head") or if the user pushes Ctrl+C.
if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:      # For example, output sent to "head".
        sys.stderr.close()
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
