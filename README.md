# 天文台夜间观测排程审计 API（Night Schedule Audit）

接收**观测日期、台址时区、目标坐标、曝光序列（滤镜 / 曝光秒数 / 重复次数）
以及望远镜能力与切换耗时**，按序推演当晚时间线：

- 计算每个目标的**可见窗口**（暗夜空 ∩ 目标高于最低观测高度）；
- 计算逐帧起止时刻、**仪器最小读出间隙**与**滤镜切换开销**；
- 校验整批序列必须在目标升落界（可见窗口）内完成、同一目标的重复曝光必须连续；
- 发现**目标不可见、能力不支持、总时长超窗、输入顺序含歧义**等问题时
  **整批拒绝（HTTP 422）并列出每个问题的位置**；
- 通过审计则返回**每帧起止时刻、切换开销与末帧状态**。

天文计算基于 [`astropy`](https://www.astropy.org/)（离线运行，无需联网下载星历）。

---

## 1. 一键启动（Docker Compose）

要求：Docker 20.10+ 与 Compose v2。

```bash
docker compose up --build -d
docker compose logs -f obs-audit
```

启动时会自动完成初始化：

1. 安装依赖、构建镜像；
2. **构建期运行单元测试**（13 项，测试失败则镜像构建失败）；
3. 容器启动 uvicorn 并轮询健康检查；
4. 健康后自动执行一次真实审计冒烟调用，日志出现
   `[entrypoint] smoke audit OK: 5 frames ...` 即代表服务可用。

停止 / 清理：

```bash
docker compose down
```

### 地址与端口

| 用途 | 地址 |
| --- | --- |
| 健康检查 | `GET http://127.0.0.1:8080/healthz` |
| 服务信息 | `GET http://127.0.0.1:8080/` |
| **排程审计** | `POST http://127.0.0.1:8080/api/v1/audit` |
| 交互式文档（Swagger） | `http://127.0.0.1:8080/docs` |
| OpenAPI 描述 | `http://127.0.0.1:8080/openapi.json` |

端口在 `docker-compose.yaml` 中配置，宿主机 8080 被占用时改成
`"18080:8080"` 即可。

---

## 2. 本地非 Docker 运行（可选）

Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
pytest -q
```

---

## 3. 调用示例

### curl

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/audit \
  -H 'Content-Type: application/json' \
  --data @examples/success_m31.json | python3 -m json.tool
```

或直接跑示例脚本（含成功与两类拒绝）：

```bash
bash examples/curl_examples.sh
python examples/call_audit.py          # 纯标准库版本
```

### 请求体（单目标简写）

```json
{
  "observation_date": "2026-10-06",
  "timezone": "Asia/Shanghai",
  "site": {"name": "兴隆", "latitude": 40.3933, "longitude": 117.5786,
           "elevation_m": 900.0},
  "target": {"name": "M31", "ra": "00:42:44.3", "dec": "+41:16:09"},
  "sequence": [
    {"filter": "V", "exposure_seconds": 120.0, "repeats": 3},
    {"filter": "R", "exposure_seconds": 60.0,  "repeats": 2}
  ],
  "telescope": {
    "filters": ["V", "R", "B"],
    "min_readout_seconds": 10.0,
    "min_exposure_seconds": 0.1,
    "max_exposure_seconds": 600.0,
    "filter_change": {"V->R": 25.0, "R->V": 25.0,
                      "V->B": 30.0, "B->V": 30.0,
                      "R->B": 30.0, "B->R": 30.0}
  },
  "min_altitude_deg": 20.0,
  "twilight": "astronomical"
}
```

多目标时改用 `targets: [...]`，且每个序列项显式带 `"target": "名称"`
（见 `examples/reject_interleaved.json`）。

### Python（标准库）

```python
import json, urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:8080/api/v1/audit",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    result = json.load(urllib.request.urlopen(req, timeout=60))
except urllib.error.HTTPError as e:
    rejected = json.load(e)   # HTTP 422 -> accepted=false, issues=[...]
```

完整脚本见 `examples/call_audit.py`。

---

## 4. 输入字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `observation_date` | 是 | 台址本地日历日 `YYYY-MM-DD`。审计**该日傍晚开始的那个夜晚**；内部锚点为本地正午 12:00 起 24h |
| `timezone` | 是 | IANA 时区，如 `Asia/Shanghai`、`UTC`、`America/Santiago` |
| `site.latitude/longitude/elevation_m` | 是 | 大地坐标（度 / 米，WGS84 椭球，北正东正） |
| `target` / `targets` | 二选一 | 单目标简写或目标列表。`ra` 接受十进制度数字或 `HH:MM:SS` / `HHhMMmSSs`；`dec` 接受十进制度数字或 `DD:MM:SS` / `DDdMMmSSs`（J2000/ICRS） |
| `sequence[]` | 是 | 曝光序列，**按序执行**：`filter`、`exposure_seconds`、`repeats`，多目标时还要 `target` |
| `telescope.filters` | 是 | 支持的滤镜集合 |
| `telescope.min_readout_seconds` | 是 | 仪器**最小读出时间**（秒，>0），相邻两帧（start−上一帧 end）不得短于此 |
| `telescope.min/max_exposure_seconds` | 否 | 单帧曝光允许范围 |
| `telescope.filter_change` | 否 | 滤镜切换耗时（秒）。标量=任意切换统一耗时；对象=`"A->B": 秒` **有向映射**；映射缺少某有向对且序列中用到时整批拒绝 |
| `min_altitude_deg` | 否 | 最低观测高度，默认 20°（0–90） |
| `twilight` | 否 | 暗夜空判定：`civil`(-6°) / `nautical`(-12°) / `astronomical`(-18°，默认) |
| `start_at` | 否 | 排程最早开始时刻，ISO8601（建议带偏移）；无时区串按 `timezone` 解释 |

未定义的多余字段会以 `AMBIGUOUS_INPUT` 拒绝（防止拼错字段被静默忽略）。

### 时间线规则

1. 每个序列项是一段**连续观测块**，`repeats` 帧连续执行；
2. 同一块内相邻帧间隙恰为 `min_readout_seconds`（最短合法情形）；
3. 相邻两块滤镜不同时，跨块间隙 = 读出 + 该有向滤镜对切换耗时；
4. 每块（含块内全部曝光）必须整体落在该目标某个**可见窗口**内
   （暗夜空 ∩ 高度 ≥ `min_altitude_deg`）；
5. 采用**最早可行贪心**：从可见窗口起点（或 `start_at`）放置当前块；
   当前窗口放不下则顺延该目标的下一个可见窗口；所有窗口都放不下 → 超窗拒绝；
6. 同一目标在序列中只能形成一个连续段，被其他目标切割 → 顺序歧义拒绝。

---

## 5. 成功响应要点（HTTP 200）

- `frames[]`：**每帧**全局序号、所属序列项、目标、滤镜、曝光秒数、
  `start_utc/end_utc`、台址本地时刻、所在窗口起止；
- `filter_changes[]`：每次切换的源/目标滤镜、耗时与起止时刻
  （同滤镜不切换；首块无切换）；
- `sequence[]`：每块汇总（起止、等待空耗、所在窗口、距窗口结束余量
  `slack_end_seconds`、是否完整在窗内）；
- `totals`：帧数、纯曝光合计、读出合计、**滤镜切换合计**、等待空耗、
  首帧→末帧挂钟时长；
- `last_frame`：**末帧状态**
  - `status`：`COMPLETED_IN_WINDOW`（有余量）/ `COMPLETED_AT_LIMIT`；
  - `remaining_window_seconds`：距该目标可见窗口结束的余量；
  - `margin_ok`：余量非负；
  - `next_readout_fits`：若再拍一帧同曝光并带最短读出，是否仍在窗内；
- `night` / `targets{}`：暗夜空段、目标高于最低高度段、可见窗口段
  （UTC 与本地时刻）；
- `constraints`：最小读出是否满足（含实测最小帧间空隙）、曝光上下限、
  连续性、全帧在窗内。

---

## 6. 整批拒绝与错误码（HTTP 422）

`{"accepted": false, "request_id": "...", "issues": [...]}`，一次性列出全部问题：

| code | 含义 |
| --- | --- |
| `INVALID_INPUT` | 时区/坐标/时间格式/数值不合法，或多目标序列项缺少 `target` |
| `AMBIGUOUS_INPUT` | 输入顺序/定义含歧义：`target` 与 `targets` 同给、目标重名、能力列表重复滤镜、出现未定义字段 |
| `ORDER_AMBIGUOUS` | 同一目标的重复曝光不连续（被其他目标切割），`detail.groups` 给出各段下标 |
| `TARGET_NOT_FOUND` | 序列项引用了不在 `targets` 中的目标 |
| `FILTER_NOT_SUPPORTED` | 序列或切换映射使用了能力集之外的滤镜 |
| `EXPOSURE_UNSUPPORTED` | 单帧曝光短于/超过仪器最短/最长曝光 |
| `FILTER_SWITCH_UNDEFINED` | 有向切换映射缺少序列中实际用到的滤镜对，无法判定时间线 |
| `TARGET_NOT_VISIBLE` | 该目标在当晚暗夜空中始终低于最低观测高度 |
| `NIGHT_UNAVAILABLE` | 该台址该日无满足晨昏蒙影条件的暗夜空（极昼等） |
| `SEQUENCE_EXCEEDS_WINDOW` | 从就绪时刻起，任何可见窗口都无法容纳该块（总时长超窗） |

每个 issue 含 `message`、`location`（JSON 路径，如 `sequence[1].filter`）、
`item_index`、`target` 与 `detail`。

拒绝响应示例：

```json
{
  "accepted": false,
  "request_id": "...",
  "issues": [
    {
      "code": "ORDER_AMBIGUOUS",
      "message": "目标 'A' 的观测被切割为 2 段，同一目标的重复曝光必须连续排列",
      "location": "sequence",
      "item_index": null,
      "target": "A",
      "detail": {"groups": [[0], [2]], "item_indexes": [0, 2]}
    }
  ]
}
```

---

## 7. 计算说明与边界

- 高度角穿越点采用 300s 网格定位 + 30 次对分细化，时间精度远优于 1ms；
- 大气折射、章动等未建模，审计用途下可忽略；坐标按 ICRS/J2000；
- 极昼（无暗夜窗）报 `NIGHT_UNAVAILABLE`；拱极天体如暗夜窗内始终高于
  最低高度，则可见窗口即暗夜空段本身；
- 曝光结束（而非读出结束）必须在窗内——读出允许延续到目标落下之后；
- 时间输出同时给 UTC（`*_utc`）与台址本地（`*_local`，带 UTC 偏移）。

## 8. 项目结构

```
app/
  astronomy.py    # 观测夜 / 晨昏蒙影 / 升落界 / 可见窗口
  schemas.py      # 请求与响应模型
  errors.py       # 审计问题与整批拒绝异常
  scheduler.py    # 校验 + 逐帧时间线推演
  main.py         # FastAPI 路由与异常映射
tests/test_audit.py
examples/         # JSON 载荷、curl 与 Python 调用示例
scripts/entrypoint.sh
Dockerfile  docker-compose.yaml  requirements.txt
```
