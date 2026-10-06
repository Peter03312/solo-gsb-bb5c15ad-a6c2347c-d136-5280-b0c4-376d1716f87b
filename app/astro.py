"""Pure-Python astronomy helpers.

Computes Greenwich Mean Sidereal Time and target rise/transit/set windows
using the standard low-precision formulas (Meeus, "Astronomical Algorithms").
Accuracy is well within a second of arc of sky motion for scheduling purposes;
no external ephemeris or third-party package is required.

All datetimes returned by this module are timezone-aware UTC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# Apparent sidereal time advances ~360.98564736629 deg per 24 h of UT.
SIDEREAL_RATE_DEG_PER_HOUR = 360.98564736629 / 24.0


def julian_date(dt: datetime) -> float:
    """Julian Date of a UTC datetime."""
    y, m = dt.year, dt.month
    frac_day = dt.day + (
        dt.hour + (dt.minute + (dt.second + dt.microsecond / 1e6) / 60.0) / 60.0
    ) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + frac_day + b - 1524.5


def gmst_deg(dt: datetime) -> float:
    """Greenwich Mean Sidereal Time in degrees, normalised to [0, 360)."""
    jd = julian_date(dt)
    t = (jd - 2451545.0) / 36525.0
    gmst = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - t * t * t / 38710000.0
    )
    return gmst % 360.0


def hour_angle_deg(dt: datetime, longitude_deg: float, ra_deg: float) -> float:
    """Hour angle (deg, -180..180) of a target at a UTC instant.

    ``longitude_deg`` is east-positive.
    """
    return (gmst_deg(dt) + longitude_deg - ra_deg + 180.0) % 360.0 - 180.0


def transit_time(approx_utc: datetime, longitude_deg: float, ra_deg: float) -> datetime:
    """Instant of upper culmination nearest to ``approx_utc`` (Newton iteration)."""
    t = approx_utc
    for _ in range(100):
        ha = hour_angle_deg(t, longitude_deg, ra_deg)
        delta_hours = -ha / SIDEREAL_RATE_DEG_PER_HOUR
        t = t + timedelta(hours=delta_hours)
        if abs(delta_hours) < 1e-10:
            break
    return t


@dataclass
class VisibilityWindow:
    """Usable observing window for one target on one night (UTC)."""

    window_start: datetime
    window_end: datetime
    rise: Optional[datetime]      # None when circumpolar w.r.t. min altitude
    set: Optional[datetime]       # None when circumpolar w.r.t. min altitude
    transit: Optional[datetime]
    circumpolar: bool


def visibility_window(
    obs_date: date,
    tz_name: str,
    latitude_deg: float,
    longitude_deg: float,
    ra_deg: float,
    dec_deg: float,
    min_altitude_deg: float,
) -> Optional[VisibilityWindow]:
    """Window during which the target is above ``min_altitude_deg``.

    The "night" is defined as the 24 h span from local noon of ``obs_date``
    to local noon of the next day, in the site timezone. The returned window
    is the intersection of the target's above-horizon interval with that span.
    Returns ``None`` when the target never reaches the required altitude.
    """
    tz = ZoneInfo(tz_name)
    noon_local = datetime(
        obs_date.year, obs_date.month, obs_date.day, 12, 0, 0, tzinfo=tz
    )
    night_start = noon_local.astimezone(timezone.utc)
    night_end = (noon_local + timedelta(hours=24)).astimezone(timezone.utc)

    sin_min = math.sin(math.radians(min_altitude_deg))
    lat_r = math.radians(latitude_deg)
    dec_r = math.radians(dec_deg)
    sin_lat, cos_lat = math.sin(lat_r), math.cos(lat_r)
    sin_dec, cos_dec = math.sin(dec_r), math.cos(dec_r)
    denom = cos_lat * cos_dec

    if abs(denom) < 1e-12:
        # Observer at a geographic pole, or target on a celestial pole:
        # altitude is constant all night.
        alt_const = 90.0 - abs(latitude_deg - dec_deg)
        if alt_const >= min_altitude_deg:
            return VisibilityWindow(night_start, night_end, None, None, None, True)
        return None

    cos_h = (sin_min - sin_lat * sin_dec) / denom
    if cos_h > 1.0:
        return None  # never climbs to the required altitude
    if cos_h < -1.0:
        # Always above the required altitude: usable all night.
        return VisibilityWindow(night_start, night_end, None, None, None, True)

    half_span_hours = (
        math.degrees(math.acos(cos_h)) / SIDEREAL_RATE_DEG_PER_HOUR
    )
    approx_midnight = night_start + timedelta(hours=12)
    transit = transit_time(approx_midnight, longitude_deg, ra_deg)
    rise = transit - timedelta(hours=half_span_hours)
    set_ = transit + timedelta(hours=half_span_hours)

    window_start = max(rise, night_start)
    window_end = min(set_, night_end)
    if window_start >= window_end:
        return None  # above the horizon only outside this night
    return VisibilityWindow(window_start, window_end, rise, set_, transit, False)
