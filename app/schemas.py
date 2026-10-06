"""HTTP 请求/响应数据模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- 请求模型


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiteInput(StrictModel):
    name: Optional[str] = Field(None, description="台址名称，仅用于回显")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="地理纬度（度，北正南负）")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="地理经度（度，东正西负）")
    elevation_m: float = Field(0.0, ge=-500.0, le=10000.0, description="海拔（米）")


class TelescopeInput(StrictModel):
    name: Optional[str] = Field(None, description="望远镜/仪器名称，仅用于回显")
    filters: list[str] = Field(..., min_length=1, description="支持的滤镜集合")
    min_readout_seconds: float = Field(..., gt=0.0, description="仪器最小读出时间（秒）")
    min_exposure_seconds: float = Field(0.0, ge=0.0, description="允许的最短单帧曝光（秒）")
    max_exposure_seconds: Optional[float] = Field(
        None, gt=0.0, description="允许的最长单帧曝光（秒）；null 表示不限")
    # 滤镜切换耗时（秒）。可以是标量（任意切换统一耗时），
    # 也可以是 {"from->to": seconds} 的有向映射；0/缺省表示零耗时。
    filter_change: Optional[Union[float, dict[str, float]]] = Field(
        None, description="滤镜切换耗时（秒）：标量或 {'A->B': 秒} 有向映射")


class TargetInput(StrictModel):
    name: str = Field(..., min_length=1, description="目标名称")
    ra: Union[float, str] = Field(
        ..., description="赤经：十进制度数字，或 HH:MM:SS / HHhMMmSSs 字符串")
    dec: Union[float, str] = Field(
        ..., description="赤纬：十进制度数字，或 DD:MM:SS / DDdMMmSSs 字符串")


class SequenceItem(StrictModel):
    target: Optional[str] = Field(
        None, description="目标名称；单目标简写（targets 为空）时省略")
    filter: str = Field(..., min_length=1, description="本块滤镜")
    exposure_seconds: float = Field(..., gt=0.0, description="单帧曝光秒数")
    repeats: int = Field(..., ge=1, description="连续重复帧数")


class AuditRequest(StrictModel):
    observation_date: date = Field(
        ..., description="观测日期（台址本地日历日），ISO YYYY-MM-DD；"
                        "审计该日期傍晚开始的那个夜晚")
    timezone: str = Field(..., description="IANA 时区名，例如 Asia/Shanghai")
    site: SiteInput
    targets: list[TargetInput] = Field(
        default_factory=list, description="目标列表；仅 1 个目标时可直接用 target 简写")
    target: Optional[TargetInput] = Field(
        None, description="单目标简写，与 targets 二选一")
    sequence: list[SequenceItem] = Field(..., min_length=1,
                                         description="曝光序列，按序执行")
    telescope: TelescopeInput
    min_altitude_deg: float = Field(20.0, ge=0.0, le=90.0,
                                    description="最低观测高度角（度）")
    twilight: Literal["civil", "nautical", "astronomical"] = Field(
        "astronomical", description="暗夜空判定：太阳低于对应晨昏蒙影高度")
    start_at: Optional[str] = Field(
        None, description="可选，排程最早开始时刻；ISO8601（含时区）或"
                          "台址本地时间串（无时区时按 timezone 解释）")


# ---------------------------------------------------------------- 响应模型


class WindowOut(BaseModel):
    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime
    duration_seconds: float


class NightOut(BaseModel):
    date_local: str
    search_start_utc: datetime
    twilight: str
    twilight_sun_altitude_deg: float
    min_altitude_deg: float
    dark_windows: list[WindowOut]
    visible_windows: list[WindowOut]
    above_horizon_windows: list[WindowOut]


class FrameOut(BaseModel):
    index: int = Field(..., description="全局帧序号（0 起）")
    item_index: int = Field(..., description="所属序列项下标（0 起）")
    target: str
    filter: str
    exposure_seconds: float
    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime
    window_start_utc: datetime
    window_end_utc: datetime


class FilterChangeOut(BaseModel):
    after_item_index: int = Field(..., description="发生在该序列项之前（首帧前）")
    target: str
    from_filter: Optional[str]
    to_filter: str
    seconds: float
    start_utc: datetime
    end_utc: datetime


class BlockSummaryOut(BaseModel):
    item_index: int
    target: str
    filter: str
    exposure_seconds: float
    repeats: int
    frames: int
    block_start_utc: datetime
    block_end_utc: datetime
    idle_before_seconds: float = Field(..., description="本块开始前等待目标升起/到点的空耗")
    window_start_utc: datetime
    window_end_utc: datetime
    within_visible_window: bool
    slack_end_seconds: float = Field(..., description="末帧结束距窗口结束的余量（负=越界）")


class LastFrameOut(BaseModel):
    status: Literal["COMPLETED_IN_WINDOW", "COMPLETED_AT_LIMIT"]
    end_utc: datetime
    end_local: datetime
    remaining_window_seconds: float = Field(..., description="距该目标可见窗口结束的余量")
    margin_ok: bool = Field(..., description="余量非负：true")
    next_readout_fits: bool = Field(..., description="若再来一帧+最短读出是否仍在窗内")


class TotalsOut(BaseModel):
    frame_count: int
    exposure_seconds: float = Field(..., description="纯曝光合计")
    readout_seconds: float = Field(..., description="读出间隙合计（首帧前不计）")
    filter_change_seconds: float = Field(..., description="序列中滤镜切换合计")
    idle_seconds: float = Field(..., description="等待升界/到点的空耗合计")
    wall_clock_seconds: float = Field(..., description="首帧开始到末帧结束的挂钟时长")


class ConstraintsOut(BaseModel):
    min_readout_seconds: float
    min_readout_respected: bool
    min_readout_min_gap_seconds: float
    min_exposure_seconds: float
    max_exposure_seconds: Optional[float]
    repeats_contiguous: bool
    all_frames_in_window: bool


class AuditResponse(BaseModel):
    accepted: bool = True
    request_id: str
    telescope: dict[str, Any]
    site: dict[str, Any]
    timezone: str
    schedule_start_utc: datetime
    schedule_end_utc: datetime
    night: NightOut
    targets: dict[str, NightOut]
    sequence: list[BlockSummaryOut]
    frames: list[FrameOut]
    filter_changes: list[FilterChangeOut]
    totals: TotalsOut
    last_frame: LastFrameOut
    constraints: ConstraintsOut


class RejectResponse(BaseModel):
    accepted: bool = False
    request_id: str
    issues: list[AuditIssue]


class HealthOut(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
