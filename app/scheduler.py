"""Core scheduling audit.

Rules enforced (all violations reject the whole batch and are reported with
their positions):

* ORDER_AMBIGUITY    - the same target name, or the same filter within one
                       target, appears in non-consecutive entries. Repeats of
                       a target must be scheduled as one contiguous block.
* FILTER_NOT_SUPPORTED - requested filter is not in the telescope capability
                       list.
* TARGET_NOT_VISIBLE - the target never reaches the site's minimum altitude
                       during the night of the observation date.
* WINDOW_EXCEEDED    - the scheduled frames of a target do not fit inside its
                       rise/set window.

Scheduling model (sequential, in the order given):

* The gap between two consecutive frames is never shorter than the
  instrument minimum readout time. When the filter changes the gap must also
  cover the filter-switch time, and when the target changes it must also
  cover the slew time, i.e. ``gap = max(min_readout, switch_or_slew)``.
* Repeats of one exposure entry are executed back-to-back (consecutive).
* A target's frames start no earlier than its window start; if the previous
  target ended before the next target rises, the schedule idles until rise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .astro import VisibilityWindow, visibility_window
from .validation import validate_payload

OVERHEAD_NONE = "none"
OVERHEAD_READOUT = "readout"
OVERHEAD_FILTER_SWITCH = "filter_switch"
OVERHEAD_SLEW = "slew"


def _iso(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).isoformat(timespec="seconds")


def _err(errors: list, position: str, code: str, message: str) -> None:
    errors.append({"position": position, "code": code, "message": message})


def _check_order_ambiguity(targets: list, errors: list) -> None:
    """Repeats of a target (and of a filter within a target) must be contiguous."""
    first_seen: dict = {}
    previous_name: Optional[str] = None
    for i, target in enumerate(targets):
        name = target["name"]
        if name in first_seen and name != previous_name:
            _err(
                errors, f"targets[{i}]", "ORDER_AMBIGUITY",
                f"target {name!r} appears again at targets[{i}] after other "
                f"targets (first occurrence targets[{first_seen[name]}]); "
                f"repeats of the same target must be consecutive",
            )
        first_seen.setdefault(name, i)
        previous_name = name

        seen_filters: dict = {}
        previous_filter: Optional[str] = None
        for j, exposure in enumerate(target["exposures"]):
            filt = exposure["filter"]
            if filt in seen_filters and filt != previous_filter:
                _err(
                    errors, f"targets[{i}].exposures[{j}]", "ORDER_AMBIGUITY",
                    f"filter {filt!r} is requested in separated blocks of "
                    f"target {name!r} (first block at "
                    f"targets[{i}].exposures[{seen_filters[filt]}]); repeated "
                    f"exposures of one filter must be consecutive",
                )
            seen_filters.setdefault(filt, j)
            previous_filter = filt


def _check_capabilities(targets: list, telescope: dict, errors: list) -> None:
    supported = set(telescope["supported_filters"])
    for i, target in enumerate(targets):
        for j, exposure in enumerate(target["exposures"]):
            if exposure["filter"] not in supported:
                _err(
                    errors, f"targets[{i}].exposures[{j}]",
                    "FILTER_NOT_SUPPORTED",
                    f"filter {exposure['filter']!r} of target "
                    f"{target['name']!r} is not supported by the telescope "
                    f"(supported: {sorted(supported)})",
                )


def _compute_windows(model: dict, errors: list) -> list:
    """One VisibilityWindow (or None) per target."""
    site = model["site"]
    windows: list = []
    for i, target in enumerate(model["targets"]):
        window = visibility_window(
            model["observation_date"],
            model["timezone"],
            site["latitude_deg"],
            site["longitude_deg"],
            target["ra_deg"],
            target["dec_deg"],
            site["min_altitude_deg"],
        )
        if window is None:
            _err(
                errors, f"targets[{i}]", "TARGET_NOT_VISIBLE",
                f"target {target['name']!r} (ra={target['ra_deg']}deg, "
                f"dec={target['dec_deg']}deg) never reaches the minimum "
                f"altitude of {site['min_altitude_deg']}deg during the night "
                f"of {model['observation_date'].isoformat()}",
            )
        windows.append(window)
    return windows


def _schedule(model: dict, windows: list, errors: list) -> list:
    """Greedy sequential schedule; appends WINDOW_EXCEEDED errors."""
    tz = ZoneInfo(model["timezone"])
    telescope = model["telescope"]
    readout = telescope["min_readout_sec"]
    switch = telescope["filter_switch_sec"]
    slew = telescope["slew_sec"]

    # Initial cursor: explicit schedule_start (naive values are read in the
    # site timezone), otherwise the first usable window start.
    start = model["schedule_start"]
    if start is not None:
        cursor = (start.replace(tzinfo=tz) if start.tzinfo is None
                  else start).astimezone(timezone.utc)
    else:
        first_window = next((w for w in windows if w is not None), None)
        cursor = first_window.window_start if first_window else None

    frames: list = []
    prev_filter: Optional[str] = None

    for i, target in enumerate(model["targets"]):
        window: Optional[VisibilityWindow] = windows[i]
        if window is None:
            continue  # already reported as TARGET_NOT_VISIBLE
        if cursor is None:
            cursor = window.window_start
        if cursor < window.window_start:
            cursor = window.window_start  # idle until the target rises

        first_frame_index = len(frames)
        first_frame_of_target = True
        for exposure in target["exposures"]:
            for repeat in range(1, exposure["count"] + 1):
                if not frames:
                    gap, kind = 0.0, OVERHEAD_NONE
                    need_switch = need_slew = 0.0
                elif first_frame_of_target:
                    gap = max(readout, slew)
                    kind = OVERHEAD_SLEW
                    need_switch, need_slew = 0.0, slew
                elif exposure["filter"] != prev_filter:
                    gap = max(readout, switch)
                    kind = OVERHEAD_FILTER_SWITCH
                    need_switch, need_slew = switch, 0.0
                else:
                    gap, kind = readout, OVERHEAD_READOUT
                    need_switch = need_slew = 0.0

                cursor = cursor + timedelta(seconds=gap)
                frame_start = cursor
                frame_end = frame_start + timedelta(seconds=exposure["exposure_sec"])
                frames.append({
                    "seq": len(frames),
                    "target": target["name"],
                    "target_index": i,
                    "filter": exposure["filter"],
                    "repeat": repeat,
                    "start": frame_start,
                    "end": frame_end,
                    "exposure_sec": exposure["exposure_sec"],
                    "gap_before_sec": gap,
                    "overhead_before": {
                        "type": kind,
                        "seconds": gap,
                        "readout_sec": readout if kind != OVERHEAD_NONE else 0.0,
                        "filter_switch_sec": need_switch,
                        "slew_sec": need_slew,
                    },
                })
                cursor = frame_end
                prev_filter = exposure["filter"]
                first_frame_of_target = False

        target_frames = frames[first_frame_index:]
        if target_frames:
            last_end = target_frames[-1]["end"]
            if last_end > window.window_end:
                overdue = (last_end - window.window_end).total_seconds()
                _err(
                    errors, f"targets[{i}]", "WINDOW_EXCEEDED",
                    f"exposure sequence of target {target['name']!r} ends "
                    f"{overdue:.1f}s after its visibility window closes "
                    f"at {_iso(window.window_end, tz)} "
                    f"({model['timezone']}); total scheduled duration "
                    f"does not fit inside the rise/set bounds",
                )

    return frames


def _render_accepted(model: dict, windows: list, frames: list) -> dict:
    tz = ZoneInfo(model["timezone"])
    overhead_totals = {
        OVERHEAD_READOUT: 0.0,
        OVERHEAD_FILTER_SWITCH: 0.0,
        OVERHEAD_SLEW: 0.0,
    }
    for frame in frames:
        kind = frame["overhead_before"]["type"]
        if kind in overhead_totals:
            overhead_totals[kind] += frame["gap_before_sec"]

    frame_list = []
    for frame in frames:
        frame_list.append({
            "seq": frame["seq"],
            "target": frame["target"],
            "target_index": frame["target_index"],
            "filter": frame["filter"],
            "repeat": frame["repeat"],
            "start": _iso(frame["start"], tz),
            "end": _iso(frame["end"], tz),
            "exposure_sec": frame["exposure_sec"],
            "gap_before_sec": frame["gap_before_sec"],
            "overhead_before": frame["overhead_before"],
        })

    per_target = []
    for i, target in enumerate(model["targets"]):
        window = windows[i]
        target_frames = [f for f in frames if f["target_index"] == i]
        entry = {
            "index": i,
            "name": target["name"],
            "circumpolar": window.circumpolar,
            "window_start": _iso(window.window_start, tz),
            "window_end": _iso(window.window_end, tz),
            "rise": _iso(window.rise, tz) if window.rise else None,
            "set": _iso(window.set, tz) if window.set else None,
            "transit": _iso(window.transit, tz) if window.transit else None,
            "frames": len(target_frames),
        }
        if target_frames:
            remaining = (window.window_end - target_frames[-1]["end"]
                         ).total_seconds()
            entry["first_frame_start"] = _iso(target_frames[0]["start"], tz)
            entry["last_frame_end"] = _iso(target_frames[-1]["end"], tz)
            entry["window_remaining_sec"] = round(remaining, 3)
        per_target.append(entry)

    last = frames[-1]
    last_window = windows[last["target_index"]]
    remaining = (last_window.window_end - last["end"]).total_seconds()
    last_frame = {
        "seq": last["seq"],
        "target": last["target"],
        "filter": last["filter"],
        "start": _iso(last["start"], tz),
        "end": _iso(last["end"], tz),
        "status": "completed_within_window",
        "window_end": _iso(last_window.window_end, tz),
        "window_remaining_sec": round(remaining, 3),
    }

    total_overhead = sum(overhead_totals.values())
    return {
        "status": "accepted",
        "observation_date": model["observation_date"].isoformat(),
        "timezone": model["timezone"],
        "summary": {
            "total_frames": len(frames),
            "total_exposure_sec": sum(f["exposure_sec"] for f in frames),
            "total_overhead_sec": total_overhead,
            "overhead": {
                "readout_sec": overhead_totals[OVERHEAD_READOUT],
                "filter_switch_sec": overhead_totals[OVERHEAD_FILTER_SWITCH],
                "slew_sec": overhead_totals[OVERHEAD_SLEW],
            },
            "first_frame_start": _iso(frames[0]["start"], tz),
            "last_frame_end": _iso(frames[-1]["end"], tz),
        },
        "targets": per_target,
        "frames": frame_list,
        "last_frame": last_frame,
    }


def run_audit(payload) -> tuple:
    """Validate and audit one request payload.

    Returns ``(http_status, body)``: 200 with the schedule when accepted,
    422 with the full list of rejection positions otherwise.
    """
    model, errors = validate_payload(payload)
    if errors:
        return 422, {"status": "rejected", "errors": errors}

    errors = []
    _check_order_ambiguity(model["targets"], errors)
    _check_capabilities(model["targets"], model["telescope"], errors)
    windows = _compute_windows(model, errors)
    frames = _schedule(model, windows, errors)

    if errors:
        return 422, {"status": "rejected", "errors": errors}
    return 200, _render_accepted(model, windows, frames)
