"""天文计算：本地观测夜、晨昏蒙影（暗夜空）与目标升落界。

所有计算基于 astropy，精度足以用于排程审计：
- 高度角穿越点用网格定位 + 对分细化（约 0.1 秒级）；
- “暗夜空”定义为太阳高度角低于指定晨昏蒙影阈值；
- “目标可见窗口”定义为目标高度角高于最低观测高度，且与暗夜空取交集。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import numpy as np
from astropy.coordinates import EarthLocation, SkyCoord, AltAz, get_sun
from astropy.time import Time
import astropy.units as u

# 晨昏蒙影类型 -> 太阳必须低于的高度角
TWILIGHT_ALT = {
    "civil": -6.0,       # 民用蒙影
    "nautical": -12.0,   # 航海蒙影
    "astronomical": -18.0,  # 天文蒙影（默认，夜间观测常用）
}

# 24 小时搜索网格步长（秒）
GRID_STEP_SECONDS = 300.0
# 对分细化迭代次数（300 / 2**30 秒，精度远超 1 毫秒）
BISECTION_STEPS = 30
EPS_SECONDS = 1e-6


class AstronomyError(ValueError):
    """输入的坐标/台址无法参与天文计算。"""


@dataclass(frozen=True)
class Site:
    location: EarthLocation
    tz: object  # zoneinfo.ZoneInfo


def build_location(latitude_deg: float, longitude_deg: float,
                   elevation_m: float) -> EarthLocation:
    return EarthLocation(
        lat=float(latitude_deg) * u.deg,
        lon=float(longitude_deg) * u.deg,
        height=float(elevation_m) * u.m,
    )


def parse_coord(ra, dec) -> SkyCoord:
    """解析目标坐标。

    ra 接受：十进制度数字（0..360）、"HH:MM:SS(.ss)" / "HHhMMmSSs" 历元时角串；
    dec 接受：十进制度数字（-90..90）、"[+-]DD:MM:SS(.ss)" / "DDdMMmSSs" 度分秒串。
    """
    # 数字输入：一律按十进制度处理（RA/Dec 一致）
    if not isinstance(ra, str):
        ra = f"{float(ra):.8f}d"
    if not isinstance(dec, str):
        dec = f"{float(dec):.8f}d"

    ra = ra.strip()
    dec = dec.strip()
    if not ra or not dec:
        raise AstronomyError("目标坐标不能为空")

    ra_unit = "hourangle" if (":" in ra or "h" in ra) else "deg"
    dec_unit = "deg"
    try:
        return SkyCoord(
            ra=ra, dec=dec,
            unit=(u.Unit(ra_unit), u.Unit(dec_unit)), frame="icrs",
        )
    except Exception:
        try:
            return SkyCoord(
                ra=ra, dec=dec,
                unit=(u.deg, u.deg), frame="icrs",
            )
        except Exception as exc:  # pragma: no cover - 由调用方归一化错误信息
            raise AstronomyError(f"无法解析目标坐标 ra={ra!r} dec={dec!r}: {exc}")


def local_noon_utc(date_any, tz) -> datetime:
    """观测日期（台址本地）正午对应的 UTC 时刻。

    取本地正午作为观测夜锚点：该日期傍晚开始的暗夜空必然落在
    [本地正午, 次日本地正午) 这 24 小时窗口内（含极昼/极夜边界）。
    """
    from datetime import date as _date
    if isinstance(date_any, _date):
        d = date_any
    else:
        d = _date.fromisoformat(str(date_any))
    noon_local = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=tz)
    return noon_local.astimezone(timezone.utc).replace(tzinfo=None)


def _alt_at(times: Time, observer: EarthLocation, body: SkyCoord) -> np.ndarray:
    altaz = body.transform_to(AltAz(obstime=times, location=observer))
    # np.atleast_1d：统一标量/数组时刻，保证调用方可按数组索引
    return np.atleast_1d(altaz.alt.to_value(u.deg))


def _threshold_intervals(t0: datetime, observer: EarthLocation, alt_fn,
                         threshold_deg: float, *, below: bool,
                         hours: float = 24.0) -> list[tuple[datetime, datetime]]:
    """返回 [t0, t0+hours] 内高度角低于/高于阈值的全部区间。

    用固定网格定位穿越点，再对每个穿越点做对分细化。
    端点本身处于阈值内时，区间按搜索窗口边界闭合。
    """
    n = int(round(hours * 3600.0 / GRID_STEP_SECONDS))
    dt_seconds = hours * 3600.0 / n
    offsets = np.arange(n + 1) * dt_seconds
    grid = Time(t0, scale="utc") + offsets * u.second
    alts = alt_fn(grid)

    inside = alts < threshold_deg if below else alts > threshold_deg
    crossings: list[float] = []
    for i in range(n):
        if inside[i] != inside[i + 1]:
            lo, hi = offsets[i], offsets[i + 1]
            for _ in range(BISECTION_STEPS):
                mid = (lo + hi) / 2.0
                a = alt_fn(Time(t0, scale="utc") + mid * u.second)[0]
                mid_inside = (a < threshold_deg) if below else (a > threshold_deg)
                if mid_inside == inside[i]:
                    lo = mid
                else:
                    hi = mid
            crossings.append((lo + hi) / 2.0)

    # 组装事件点：0 与 hours 为窗口端点，其余为穿越点
    events = [0.0]
    events.extend(crossings)
    events.append(hours * 3600.0)

    intervals: list[tuple[datetime, datetime]] = []
    for i in range(len(events) - 1):
        mid_off = (events[i] + events[i + 1]) / 2.0
        a = alt_fn(Time(t0, scale="utc") + mid_off * u.second)[0]
        mid_inside = (a < threshold_deg) if below else (a > threshold_deg)
        if mid_inside:
            start = t0 + timedelta(seconds=events[i] + EPS_SECONDS)
            end = t0 + timedelta(seconds=events[i + 1] - EPS_SECONDS)
            if end > start:
                intervals.append((start, end))
    return intervals


def _intersect(a: Iterable[tuple[datetime, datetime]],
               b: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """两组已排序、互不相交时间区间的交集。"""
    a, b = list(a), list(b)
    out: list[tuple[datetime, datetime]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


@dataclass
class NightWindows:
    date_local: str
    search_start_utc: datetime
    dark: list[tuple[datetime, datetime]]
    target: list[tuple[datetime, datetime]]
    visible: list[tuple[datetime, datetime]]


def compute_night_windows(
    *,
    date_any,
    tz,
    observer: EarthLocation,
    target: SkyCoord,
    min_altitude_deg: float,
    twilight: str,
) -> NightWindows:
    """计算某观测夜的暗夜空段、目标在地高度段、以及二者交集（可见窗口）。"""
    from datetime import date as _date
    if isinstance(date_any, _date):
        date_str = date_any.isoformat()
    else:
        date_str = str(date_any)

    t0 = local_noon_utc(date_str, tz)

    def sun_alt(times: Time) -> np.ndarray:
        return _alt_at(times, observer, get_sun(times))

    def target_alt(times: Time) -> np.ndarray:
        return _alt_at(times, observer, target)

    sun_limit = TWILIGHT_ALT[twilight]
    dark = _threshold_intervals(
        t0, observer, sun_alt, sun_limit, below=True, hours=24.0,
    )
    if not dark:
        # 极昼等：该本地日期对应的 24h 内没有足够暗的夜空
        return NightWindows(date_str, t0, [], [], [])

    up = _threshold_intervals(
        t0, observer, target_alt, float(min_altitude_deg), below=False,
        hours=24.0,
    )
    visible = _intersect(dark, up)
    return NightWindows(date_str, t0, dark, up, visible)
