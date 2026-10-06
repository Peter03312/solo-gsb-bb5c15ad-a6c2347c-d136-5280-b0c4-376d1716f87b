"""Request payload validation.

Turns the decoded JSON body into a normalised model dict. Every problem is
reported as ``{"position", "code", "message"}`` where ``position`` locates the
offending element, e.g. ``targets[0].exposures[1].exposure_sec``. All schema
problems are collected before returning, so a rejected batch lists every
position at once.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

MAX_EXPOSURE_SEC = 86400.0
MAX_REPEAT_COUNT = 100000


def _err(errors: list, position: str, message: str, code: str = "INVALID_INPUT") -> None:
    errors.append({"position": position, "code": code, "message": message})


def _is_number(value: Any) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _req_str(data: dict, key: str, pos: str, errors: list,
             max_len: int = 256) -> Optional[str]:
    if key not in data:
        _err(errors, pos, f"missing required field '{key}'")
        return None
    value = data[key]
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        _err(errors, f"{pos}.{key}", f"'{key}' must be a non-empty string "
                                     f"(<= {max_len} chars)")
        return None
    return value


def _req_number(data: dict, key: str, pos: str, errors: list,
                lo: float, hi: float, lo_open: bool = False,
                hi_open: bool = False) -> Optional[float]:
    if key not in data:
        _err(errors, pos, f"missing required field '{key}'")
        return None
    value = data[key]
    if not _is_number(value):
        _err(errors, f"{pos}.{key}", f"'{key}' must be a number")
        return None
    value = float(value)
    bad_lo = value <= lo if lo_open else value < lo
    bad_hi = value >= hi if hi_open else value > hi
    if bad_lo or bad_hi:
        lo_b = "(" if lo_open else "["
        hi_b = ")" if hi_open else "]"
        _err(errors, f"{pos}.{key}",
             f"'{key}' must be in range {lo_b}{lo}, {hi}{hi_b}")
        return None
    return value


def _opt_number(data: dict, key: str, pos: str, errors: list,
                lo: float, hi: float, default: float) -> float:
    if key not in data or data[key] is None:
        return default
    value = _req_number(data, key, pos, errors, lo, hi)
    return default if value is None else value


def _parse_date(value: Any, pos: str, errors: list) -> Optional[date]:
    if not isinstance(value, str):
        _err(errors, pos, "'observation_date' must be a string 'YYYY-MM-DD'")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _err(errors, pos,
             f"'observation_date' must be a valid date 'YYYY-MM-DD', got {value!r}")
        return None


def _parse_datetime(value: Any, pos: str, errors: list) -> Optional[datetime]:
    if not isinstance(value, str):
        _err(errors, pos, "'schedule_start' must be an ISO 8601 string")
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        _err(errors, pos,
             f"'schedule_start' must be ISO 8601, e.g. '2026-10-06T20:30:00', "
             f"got {value!r}")
        return None


def _validate_exposure(raw: Any, pos: str, errors: list) -> Optional[dict]:
    if not isinstance(raw, dict):
        _err(errors, pos, "exposure entry must be an object")
        return None
    ok = True
    filt = _req_str(raw, "filter", pos, errors, max_len=32)
    ok &= filt is not None
    expo = _req_number(raw, "exposure_sec", pos, errors, 0.0, MAX_EXPOSURE_SEC,
                       lo_open=True)
    ok &= expo is not None
    count = None
    if "count" not in raw:
        _err(errors, pos, "missing required field 'count'")
        ok = False
    elif not _is_int(raw["count"]) or not (1 <= raw["count"] <= MAX_REPEAT_COUNT):
        _err(errors, f"{pos}.count",
             f"'count' must be an integer in [1, {MAX_REPEAT_COUNT}]")
        ok = False
    else:
        count = int(raw["count"])
    if not ok:
        return None
    return {"filter": filt, "exposure_sec": expo, "count": count}


def _validate_target(raw: Any, pos: str, errors: list) -> Optional[dict]:
    if not isinstance(raw, dict):
        _err(errors, pos, "target entry must be an object")
        return None
    ok = True
    name = _req_str(raw, "name", pos, errors, max_len=128)
    ok &= name is not None
    ra = _req_number(raw, "ra_deg", pos, errors, 0.0, 360.0, hi_open=True)
    ok &= ra is not None
    dec = _req_number(raw, "dec_deg", pos, errors, -90.0, 90.0)
    ok &= dec is not None

    exposures = []
    if "exposures" not in raw:
        _err(errors, pos, "missing required field 'exposures'")
        ok = False
    elif not isinstance(raw["exposures"], list) or not raw["exposures"]:
        _err(errors, f"{pos}.exposures", "'exposures' must be a non-empty array")
        ok = False
    else:
        for j, item in enumerate(raw["exposures"]):
            entry = _validate_exposure(item, f"{pos}.exposures[{j}]", errors)
            if entry is not None:
                exposures.append(entry)
        if len(exposures) != len(raw["exposures"]):
            ok = False
    if not ok:
        return None
    return {"name": name, "ra_deg": ra, "dec_deg": dec, "exposures": exposures}


def validate_payload(data: Any):
    """Validate the decoded JSON body.

    Returns ``(model, errors)``; ``model`` is ``None`` when errors exist.
    """
    errors: list = []
    if not isinstance(data, dict):
        _err(errors, "$", "request body must be a JSON object")
        return None, errors

    # --- observation_date -------------------------------------------------
    obs_date = None
    if "observation_date" not in data:
        _err(errors, "$", "missing required field 'observation_date'")
    else:
        obs_date = _parse_date(data["observation_date"],
                               "$.observation_date", errors)

    # --- timezone -----------------------------------------------------------
    tz_name = None
    if "timezone" not in data:
        _err(errors, "$", "missing required field 'timezone'")
    elif not isinstance(data["timezone"], str):
        _err(errors, "$.timezone",
             "'timezone' must be an IANA name, e.g. 'America/Santiago'")
    else:
        try:
            ZoneInfo(data["timezone"])
            tz_name = data["timezone"]
        except Exception:
            _err(errors, "$.timezone",
                 f"unknown IANA timezone: {data['timezone']!r}")

    # --- site ---------------------------------------------------------------
    site = None
    if "site" not in data:
        _err(errors, "$", "missing required field 'site'")
    elif not isinstance(data["site"], dict):
        _err(errors, "$.site", "'site' must be an object")
    else:
        raw_site = data["site"]
        lat = _req_number(raw_site, "latitude_deg", "$.site", errors, -90.0, 90.0)
        lon = _req_number(raw_site, "longitude_deg", "$.site", errors, -180.0, 180.0)
        min_alt = _opt_number(raw_site, "min_altitude_deg", "$.site", errors,
                              -90.0, 90.0, 0.0)
        if lat is not None and lon is not None:
            site = {"latitude_deg": lat, "longitude_deg": lon,
                    "min_altitude_deg": min_alt}

    # --- schedule_start (optional) -----------------------------------------
    schedule_start = None
    if data.get("schedule_start") is not None:
        schedule_start = _parse_datetime(data["schedule_start"],
                                         "$.schedule_start", errors)

    # --- targets -------------------------------------------------------------
    targets = []
    if "targets" not in data:
        _err(errors, "$", "missing required field 'targets'")
    elif not isinstance(data["targets"], list) or not data["targets"]:
        _err(errors, "$.targets", "'targets' must be a non-empty array")
    else:
        for i, item in enumerate(data["targets"]):
            target = _validate_target(item, f"targets[{i}]", errors)
            if target is not None:
                targets.append(target)

    # --- telescope -----------------------------------------------------------
    telescope = None
    if "telescope" not in data:
        _err(errors, "$", "missing required field 'telescope'")
    elif not isinstance(data["telescope"], dict):
        _err(errors, "$.telescope", "'telescope' must be an object")
    else:
        raw_tel = data["telescope"]
        ok = True
        filters = raw_tel.get("supported_filters")
        if (not isinstance(filters, list) or not filters
                or any(not isinstance(f, str) or not f.strip() for f in filters)):
            _err(errors, "$.telescope.supported_filters",
                 "'supported_filters' must be a non-empty array of strings")
            ok = False
        switch = _opt_number(raw_tel, "filter_switch_sec", "$.telescope", errors,
                             0.0, MAX_EXPOSURE_SEC, 0.0)
        readout = _opt_number(raw_tel, "min_readout_sec", "$.telescope", errors,
                              0.0, MAX_EXPOSURE_SEC, 0.0)
        slew = _opt_number(raw_tel, "slew_sec", "$.telescope", errors,
                           0.0, MAX_EXPOSURE_SEC, 0.0)
        if ok:
            telescope = {
                "supported_filters": [f.strip() for f in filters],
                "filter_switch_sec": switch,
                "min_readout_sec": readout,
                "slew_sec": slew,
            }

    if errors:
        return None, errors
    return {
        "observation_date": obs_date,
        "timezone": tz_name,
        "site": site,
        "schedule_start": schedule_start,
        "targets": targets,
        "telescope": telescope,
    }, []
