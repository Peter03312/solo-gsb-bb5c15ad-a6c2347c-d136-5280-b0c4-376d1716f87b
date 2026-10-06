"""排程审计核心：输入校验、可见窗口计算、逐帧时间线推演。

时间线规则（与 README 一致）：
- 每个序列项是目标的一段连续观测块，repeats 帧在块内连续执行；
- 相邻两帧之间至少留 min_readout_seconds 的仪器读出间隙；
- 相邻两块滤镜不同且切换耗时 >0 时，在读出之后追加滤镜切换耗时；
- 块必须整体落在该目标“可见窗口”（暗夜空 ∩ 目标高于最低高度）内；
- 采用最早可行贪心：从可见窗口起点（或 start_at）起逐块放置，
  放不下则顺延到该目标下一个可见窗口，全部窗口都放不下即整批拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astropy.coordinates import SkyCoord

from . import astronomy as astro
from .errors import AuditError, AuditIssue, issue
from .schemas import (
    AuditRequest, AuditResponse, BlockSummaryOut, ConstraintsOut,
    FilterChangeOut, FrameOut, LastFrameOut, NightOut, TotalsOut, WindowOut,
)

EPS = 1e-6  # 秒级比较容差


# ---------------------------------------------------------------- 工具函数


def _dt(t: datetime) -> datetime:
    """统一为 UTC aware datetime，供 JSON 序列化。"""
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _local(t: datetime, tz) -> datetime:
    return _dt(t).astimezone(tz)


def _window_out(w: tuple[datetime, datetime], tz) -> WindowOut:
    start, end = w
    return WindowOut(
        start_utc=_dt(start), end_utc=_dt(end),
        start_local=_local(start, tz), end_local=_local(end, tz),
        duration_seconds=(end - start).total_seconds(),
    )


# ---------------------------------------------------------------- 输入校验


@dataclass
class Prepared:
    tz: ZoneInfo
    targets: dict[str, object]          # name -> TargetInput
    coords: dict[str, SkyCoord]
    observer: object
    switch_lookup: object              # callable(from_filter, to_filter) -> seconds
    start_utc: Optional[datetime]
    twilight: str
    min_altitude_deg: float


def _build_switch_lookup(telescope, issues: list[AuditIssue]):
    raw = telescope.filter_change
    default: Optional[float]
    matrix: dict[tuple[str, str], float] = {}

    if raw is None:
        default = 0.0
    elif isinstance(raw, (int, float)):
        if raw < 0:
            issues.append(issue(
                "INVALID_INPUT", "滤镜切换耗时不能为负",
                "telescope.filter_change"))
        default = float(raw)
    else:
        default = 0.0
        known = set(telescope.filters)
        for key, val in raw.items():
            loc = f"telescope.filter_change.{key}"
            if "->" not in key:
                issues.append(issue(
                    "INVALID_INPUT",
                    "滤镜切换映射的键必须形如 'V->R'（from->to）", loc))
                continue
            f_from, f_to = (s.strip() for s in key.split("->", 1))
            if f_from not in known or f_to not in known:
                issues.append(issue(
                    "FILTER_NOT_SUPPORTED",
                    f"切换映射 {key!r} 引用了不在 filters 能力集内的滤镜", loc))
                continue
            if not isinstance(val, (int, float)) or val < 0:
                issues.append(issue(
                    "INVALID_INPUT", f"切换 {key!r} 的耗时必须是非负数字", loc))
                continue
            matrix[(f_from, f_to)] = float(val)

    def lookup(f_from: Optional[str], f_to: str) -> Optional[float]:
        if f_from == f_to:
            return 0.0
        if f_from is None:
            return 0.0
        if isinstance(raw, dict):
            val = matrix.get((f_from, f_to))
            return val  # None 表示未定义
        return default

    return lookup


def _parse_start_at(raw: Optional[str], tz, t0: datetime,
                    issues: list[AuditIssue]) -> Optional[datetime]:
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        issues.append(issue(
            "INVALID_INPUT",
            "start_at 必须是 ISO8601 时间（如 2026-10-06T19:00:00+08:00）",
            "start_at"))
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if not (t0 - timedelta(hours=12) <= dt_utc <= t0 + timedelta(hours=36)):
        issues.append(issue(
            "INVALID_INPUT",
            "start_at 与观测日期相差过远，应位于该观测夜前后",
            "start_at", detail={"utc": _dt(dt_utc).isoformat()}))
    return dt_utc


def validate(req: AuditRequest) -> Prepared:
    issues: list[AuditIssue] = []

    # 时区
    try:
        tz = ZoneInfo(req.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        issues.append(issue(
            "INVALID_INPUT",
            f"无法识别的 IANA 时区: {req.timezone!r}", "timezone"))
        tz = timezone.utc

    # 目标集合归一化
    targets: dict[str, object] = {}
    single_name: Optional[str] = None
    if req.target is not None and req.targets:
        issues.append(issue(
            "AMBIGUOUS_INPUT",
            "target 与 targets 同时给出，目标来源存在歧义；请只保留一种",
            "target/targets"))
        # 仍按 targets 继续校验，避免级联出大量 TARGET_NOT_FOUND 噪声
        for j, tj in enumerate(req.targets):
            if tj.name in targets:
                issues.append(issue(
                    "AMBIGUOUS_INPUT", f"目标名称 {tj.name!r} 重复",
                    f"targets[{j}].name", target=tj.name))
            targets[tj.name] = tj
    elif req.target is not None:
        single_name = req.target.name
        targets = {req.target.name: req.target}
    elif req.targets:
        for i, t in enumerate(req.targets):
            if t.name in targets:
                issues.append(issue(
                    "AMBIGUOUS_INPUT", f"目标名称 {t.name!r} 重复",
                    f"targets[{i}].name", target=t.name))
            targets[t.name] = t
    else:
        issues.append(issue(
            "INVALID_INPUT", "必须提供 target（简写）或 targets 列表",
            "targets"))

    # 序列项归属与能力校验
    multi = bool(req.targets)
    for i, item in enumerate(req.sequence):
        if multi:
            if not item.target:
                issues.append(issue(
                    "INVALID_INPUT",
                    "多目标模式下每个序列项必须显式给出 target",
                    f"sequence[{i}].target", item_index=i))
            elif item.target not in targets:
                issues.append(issue(
                    "TARGET_NOT_FOUND",
                    f"序列项引用的目标 {item.target!r} 不在 targets 中",
                    f"sequence[{i}].target", item_index=i,
                    target=item.target))
        else:
            if item.target and single_name and item.target != single_name:
                issues.append(issue(
                    "TARGET_NOT_FOUND",
                    f"单目标简写模式下只允许目标 {single_name!r}，"
                    f"却引用了 {item.target!r}",
                    f"sequence[{i}].target", item_index=i,
                    target=item.target))
            if single_name is not None:
                item.target = single_name

        if item.filter not in req.telescope.filters:
            issues.append(issue(
                "FILTER_NOT_SUPPORTED",
                f"滤镜 {item.filter!r} 不在望远镜能力集 "
                f"{req.telescope.filters} 内",
                f"sequence[{i}].filter", item_index=i,
                target=item.target, detail={"filter": item.filter}))

        if item.exposure_seconds < req.telescope.min_exposure_seconds - EPS:
            issues.append(issue(
                "EXPOSURE_UNSUPPORTED",
                f"曝光 {item.exposure_seconds:g}s 短于仪器最短曝光 "
                f"{req.telescope.min_exposure_seconds:g}s",
                f"sequence[{i}].exposure_seconds", item_index=i,
                target=item.target,
                detail={"exposure_seconds": item.exposure_seconds,
                        "min_exposure_seconds":
                            req.telescope.min_exposure_seconds}))
        mx = req.telescope.max_exposure_seconds
        if mx is not None and item.exposure_seconds > mx + EPS:
            issues.append(issue(
                "EXPOSURE_UNSUPPORTED",
                f"曝光 {item.exposure_seconds:g}s 超过仪器最长曝光 {mx:g}s",
                f"sequence[{i}].exposure_seconds", item_index=i,
                target=item.target,
                detail={"exposure_seconds": item.exposure_seconds,
                        "max_exposure_seconds": mx}))

    # 望远镜能力列表中的重复滤镜
    if len(set(req.telescope.filters)) != len(req.telescope.filters):
        issues.append(issue(
            "AMBIGUOUS_INPUT", "telescope.filters 中存在重复滤镜，能力定义有歧义",
            "telescope.filters"))

    # 同一目标的重复次数必须连续：按目标切分连续组，出现 2 组以上即拒绝
    groups: dict[str, list[list[int]]] = {}
    for i, item in enumerate(req.sequence):
        name = item.target
        if name not in groups or (groups[name] and groups[name][-1][-1] != i - 1):
            groups.setdefault(name, []).append([])
        groups[name][-1].append(i)
    for name, grps in groups.items():
        if name and len(grps) > 1:
            all_idx = [i for g in grps for i in g]
            issues.append(issue(
                "ORDER_AMBIGUOUS",
                f"目标 {name!r} 的观测被切割为 {len(grps)} 段，"
                "同一目标的重复曝光必须连续排列",
                "sequence", target=name,
                detail={"groups": grps, "item_indexes": all_idx}))

    switch_lookup = _build_switch_lookup(req.telescope, issues)

    t0 = astro.local_noon_utc(req.observation_date, tz)
    start_utc = _parse_start_at(req.start_at, tz, t0, issues)

    if issues:
        raise AuditError(issues)

    # 坐标与台址
    coords: dict[str, SkyCoord] = {}
    for name, t in targets.items():
        try:
            coords[name] = astro.parse_coord(t.ra, t.dec)
        except astro.AstronomyError as exc:
            issues.append(issue(
                "INVALID_INPUT", f"目标 {name!r} 坐标无法解析：{exc}",
                f"targets.{name}", target=name))
    try:
        observer = astro.build_location(
            req.site.latitude, req.site.longitude, req.site.elevation_m)
    except Exception as exc:
        issues.append(issue(
            "INVALID_INPUT", f"台址坐标无法使用：{exc}", "site"))

    if issues:
        raise AuditError(issues)

    return Prepared(
        tz=tz, targets=targets, coords=coords, observer=observer,
        switch_lookup=switch_lookup, start_utc=start_utc,
        twilight=req.twilight, min_altitude_deg=req.min_altitude_deg,
    )


# ---------------------------------------------------------------- 时间线推演


@dataclass
class _BlockResult:
    summary: BlockSummaryOut
    frames: list[FrameOut]
    change: Optional[FilterChangeOut]
    switch_seconds: float
    readout_seconds: float
    idle_seconds: float
    exposure_seconds: float
    block_end: datetime
    window: tuple[datetime, datetime]


def _fit_window(windows, earliest, span):
    """在目标可见窗口中找最早可容纳 span 秒的窗口。"""
    for w in windows:
        if earliest is not None and w[1] <= earliest + EPS:
            continue
        start = max(w[0], earliest) if earliest is not None else w[0]
        if start + span <= w[1] + EPS:
            return w, start
    return None, None


def run_audit(req: AuditRequest, request_id: str) -> AuditResponse:
    prep = validate(req)
    tz = prep.tz
    readout = req.telescope.min_readout_seconds

    # 逐目标计算可见窗口
    nights: dict[str, astro.NightWindows] = {}
    issues: list[AuditIssue] = []
    for name, t in prep.targets.items():
        nw = astro.compute_night_windows(
            date_any=req.observation_date, tz=tz, observer=prep.observer,
            target=prep.coords[name], min_altitude_deg=prep.min_altitude_deg,
            twilight=prep.twilight)
        nights[name] = nw
        if not nw.dark:
            issues.append(issue(
                "NIGHT_UNAVAILABLE",
                f"观测日 {req.observation_date} 在该台址没有符合"
                f"{prep.twilight} 晨昏蒙影条件的暗夜空（可能处于极昼）",
                "observation_date", target=name))
        elif not nw.visible:
            issues.append(issue(
                "TARGET_NOT_VISIBLE",
                f"目标 {name!r} 在该观测夜的暗夜空中始终低于 "
                f"{prep.min_altitude_deg:g}° 观测高度，无可见窗口",
                "sequence", target=name))
    if issues:
        raise AuditError(issues)

    blocks: list[_BlockResult] = []
    global_frames: list[FrameOut] = []
    changes: list[FilterChangeOut] = []

    exposure_total = 0.0
    readout_total = 0.0
    switch_total = 0.0
    idle_total = 0.0

    prev_end: Optional[datetime] = None
    prev_filter: Optional[str] = None
    frame_index = 0

    for i, item in enumerate(req.sequence):
        name = item.target
        windows = nights[name].visible
        span = (item.repeats - 1) * (item.exposure_seconds + readout) \
            + item.exposure_seconds

        # 进入本块前的滤镜切换（紧接前一末帧读出之后）
        switch_seconds = prep.switch_lookup(prev_filter, item.filter)
        if switch_seconds is None:
            raise AuditError([issue(
                "FILTER_SWITCH_UNDEFINED",
                f"从滤镜 {prev_filter!r} 切换到 {item.filter!r} 的耗时"
                "未在 filter_change 映射中定义，无法判定时间线",
                f"sequence[{i}].filter", item_index=i, target=name,
                detail={"from_filter": prev_filter, "to_filter": item.filter})])

        if i == 0:
            earliest = prep.start_utc
        else:
            earliest = prev_end + timedelta(
                seconds=readout + switch_seconds)

        window, start = _fit_window(windows, earliest, span)
        if window is None:
            ready_desc = ("可见窗口起点" if earliest is None
                          else _dt(earliest).isoformat())
            windows_payload = [
                {"start_utc": _dt(w0).isoformat(),
                 "end_utc": _dt(w1).isoformat()}
                for w0, w1 in windows]
            raise AuditError([issue(
                "SEQUENCE_EXCEEDS_WINDOW",
                f"sequence[{i}]（目标 {name}，{item.repeats}×"
                f"{item.exposure_seconds:g}s/{item.filter}，共需 "
                f"{span:g}s）无法在其可见窗口内完成（就绪时刻："
                f"{ready_desc}）",
                f"sequence[{i}]", item_index=i, target=name,
                detail={"required_seconds": span,
                        "ready_utc": None if earliest is None
                        else _dt(earliest).isoformat(),
                        "windows": windows_payload})])

        reference = earliest if earliest is not None else window[0]
        idle = max(0.0, (start - reference).total_seconds())
        idle_total += idle

        # 逐帧
        frame_objs: list[FrameOut] = []
        t = start
        for k in range(item.repeats):
            f_start = t
            f_end = t + timedelta(seconds=item.exposure_seconds)
            frame_objs.append(FrameOut(
                index=frame_index, item_index=i, target=name,
                filter=item.filter, exposure_seconds=item.exposure_seconds,
                start_utc=_dt(f_start), end_utc=_dt(f_end),
                start_local=_local(f_start, tz), end_local=_local(f_end, tz),
                window_start_utc=_dt(window[0]),
                window_end_utc=_dt(window[1])))
            frame_index += 1
            t = f_end + timedelta(seconds=readout)
        block_end = frame_objs[-1].end_utc.replace(tzinfo=None)

        if block_end > window[1] + EPS:  # 防御性检查（_fit_window 已保证）
            raise AuditError([issue(
                "SEQUENCE_EXCEEDS_WINDOW",
                f"sequence[{i}] 末帧结束时刻越过目标 {name} 的可见窗口结束界",
                f"sequence[{i}]", item_index=i, target=name)])

        change_obj = None
        # 同滤镜相邻块不产生滤镜切换（仅有读出间隙）
        if i > 0 and prev_filter != item.filter and switch_seconds > 0:
            c_start = prev_end + timedelta(seconds=readout)
            c_end = c_start + timedelta(seconds=switch_seconds)
            change_obj = FilterChangeOut(
                after_item_index=i, target=name,
                from_filter=prev_filter, to_filter=item.filter,
                seconds=switch_seconds,
                start_utc=_dt(c_start), end_utc=_dt(c_end))
            changes.append(change_obj)
            switch_total += switch_seconds

        slack = (window[1] - block_end).total_seconds()
        summary = BlockSummaryOut(
            item_index=i, target=name, filter=item.filter,
            exposure_seconds=item.exposure_seconds, repeats=item.repeats,
            frames=item.repeats,
            block_start_utc=_dt(start), block_end_utc=_dt(block_end),
            idle_before_seconds=idle,
            window_start_utc=_dt(window[0]), window_end_utc=_dt(window[1]),
            within_visible_window=block_end <= window[1] + EPS,
            slack_end_seconds=slack)

        blocks.append(_BlockResult(
            summary=summary, frames=frame_objs, change=change_obj,
            switch_seconds=switch_seconds,
            readout_seconds=max(0, item.repeats - 1) * readout,
            idle_seconds=idle,
            exposure_seconds=item.repeats * item.exposure_seconds,
            block_end=block_end, window=window))
        global_frames.extend(frame_objs)

        exposure_total += item.repeats * item.exposure_seconds
        readout_total += max(0, item.repeats - 1) * readout
        prev_end = block_end
        prev_filter = item.filter

    # 跨块读出（每两个相邻块之间 1 次）
    cross_block_readout = max(0, len(blocks) - 1) * readout
    readout_total += cross_block_readout

    first_start = global_frames[0].start_utc.replace(tzinfo=None)
    last_end = global_frames[-1].end_utc.replace(tzinfo=None)
    last = global_frames[-1]

    # 实测最小帧间空隙（末帧结束 -> 下一帧开始）
    min_gap = None
    for a, b in zip(global_frames, global_frames[1:]):
        gap = (b.start_utc.replace(tzinfo=None)
               - a.end_utc.replace(tzinfo=None)).total_seconds()
        min_gap = gap if min_gap is None else min(min_gap, gap)

    # 若再来一帧（同曝光）+最短读出，是否仍在窗内
    last_window_end = blocks[-1].window[1]
    next_frame_end = last_end + timedelta(
        seconds=readout + last.exposure_seconds)
    remaining = (last_window_end - last_end).total_seconds()

    def _night_out(nw: astro.NightWindows) -> NightOut:
        return NightOut(
            date_local=nw.date_local,
            search_start_utc=_dt(nw.search_start_utc),
            twilight=prep.twilight,
            twilight_sun_altitude_deg=astro.TWILIGHT_ALT[prep.twilight],
            min_altitude_deg=prep.min_altitude_deg,
            dark_windows=[_window_out(w, tz) for w in nw.dark],
            visible_windows=[_window_out(w, tz) for w in nw.visible],
            above_horizon_windows=[_window_out(w, tz) for w in nw.target])

    first_name = req.sequence[0].target
    targets_night = {name: _night_out(nw) for name, nw in nights.items()}

    return AuditResponse(
        accepted=True, request_id=request_id,
        telescope={"name": req.telescope.name,
                   "filters": req.telescope.filters,
                   "min_readout_seconds": readout,
                   "filter_change": req.telescope.filter_change},
        site={"name": req.site.name,
              "latitude": req.site.latitude,
              "longitude": req.site.longitude,
              "elevation_m": req.site.elevation_m},
        timezone=req.timezone,
        schedule_start_utc=_dt(first_start),
        schedule_end_utc=_dt(last_end),
        night=targets_night[first_name],
        targets=targets_night,
        sequence=[b.summary for b in blocks],
        frames=global_frames,
        filter_changes=changes,
        totals=TotalsOut(
            frame_count=len(global_frames),
            exposure_seconds=exposure_total,
            readout_seconds=readout_total,
            filter_change_seconds=switch_total,
            idle_seconds=idle_total,
            wall_clock_seconds=(last_end - first_start).total_seconds()),
        last_frame=LastFrameOut(
            status=("COMPLETED_IN_WINDOW" if remaining > 1e-3
                    else "COMPLETED_AT_LIMIT"),
            end_utc=last.end_utc, end_local=last.end_local,
            remaining_window_seconds=remaining,
            margin_ok=remaining >= -EPS,
            next_readout_fits=next_frame_end <= last_window_end + EPS),
        constraints=ConstraintsOut(
            min_readout_seconds=readout,
            min_readout_respected=(min_gap is None or min_gap + EPS >= readout),
            min_readout_min_gap_seconds=(min_gap if min_gap is not None
                                         else readout),
            min_exposure_seconds=req.telescope.min_exposure_seconds,
            max_exposure_seconds=req.telescope.max_exposure_seconds,
            repeats_contiguous=True,
            all_frames_in_window=all(
                b.summary.within_visible_window for b in blocks)))
