# Observatory Night Scheduler Audit API

用于天文台夜间观测排程审计的 HTTP API。输入观测日期、台址时区、目标坐标、
曝光序列（滤镜 / 曝光秒数 / 重复次数）与望远镜能力和切换耗时，服务按序
计算目标可见窗口与滤镜切换占用，逐帧排出时刻表；发现违规时**整批拒绝**
并列出全部出错位置。

纯 Python 标准库实现（Python ≥ 3.11），无第三方运行时依赖；天文计算
（GMST 恒星时、目标升/落/中天时刻）基于 Meeus 公式自实现。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

启动即完成初始化（IANA 时区数据库自检），容器内置健康检查。

- 服务地址： **http://localhost:8000**
- 健康检查： `GET http://localhost:8000/health`
- 审计接口： `POST http://localhost:8000/api/v1/schedule/audit`

也可以只用 Docker：

```bash
docker build -t obs-scheduler-audit .
docker run -d -p 8000:8000 --name obs-scheduler-audit obs-scheduler-audit
```

## 配置（环境变量）

| 变量        | 默认值    | 说明                     |
| ----------- | --------- | ------------------------ |
| `HOST`      | `0.0.0.0` | 监听地址                 |
| `PORT`      | `8000`    | 监听端口                 |
| `LOG_LEVEL` | `INFO`    | 日志级别                 |

修改宿主机映射端口：编辑 `docker-compose.yml` 的 `ports`，如 `"18000:8000"`。

## API

### `POST /api/v1/schedule/audit`

请求体：

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `observation_date` | string `YYYY-MM-DD` | 观测日期（当地历日，夜间定义为当日正午至次日正午） |
| `timezone` | string | 台址 IANA 时区名，如 `America/Santiago` |
| `site.latitude_deg` | number [-90, 90] | 台址纬度（度） |
| `site.longitude_deg` | number [-180, 180] | 台址经度（度，东经为正） |
| `site.min_altitude_deg` | number，默认 0 | 最低可用地平高度（度），升落界按此计算 |
| `schedule_start` | string，可选 | 排程起点 ISO 8601；无时区按台址时区解释；缺省从首个目标窗口起点开始 |
| `targets[]` | array，非空 | 目标列表，按给定顺序排程 |
| `targets[].name` | string | 目标名 |
| `targets[].ra_deg` | number [0, 360) | 赤经（度） |
| `targets[].dec_deg` | number [-90, 90] | 赤纬（度） |
| `targets[].exposures[]` | array，非空 | 曝光序列，按序执行 |
| `targets[].exposures[].filter` | string | 滤镜名 |
| `targets[].exposures[].exposure_sec` | number (0, 86400] | 单帧曝光秒数 |
| `targets[].exposures[].count` | int [1, 100000] | 重复次数（连续执行） |
| `telescope.supported_filters` | string[] | 望远镜支持的滤镜（能力） |
| `telescope.filter_switch_sec` | number ≥ 0，默认 0 | 滤镜切换耗时 |
| `telescope.min_readout_sec` | number ≥ 0，默认 0 | 仪器最小读出时间 |
| `telescope.slew_sec` | number ≥ 0，默认 0 | 目标间转台耗时 |

### 排程与审计规则

1. 按输入顺序逐目标、逐曝光项排程；同一曝光项的 `count` 次重复**连续**执行。
2. 每个目标的全部帧必须落在其升落窗口内（高度 ≥ `min_altitude_deg`）。
3. 相邻帧间隔不短于 `min_readout_sec`；换滤镜时间隔还须覆盖
   `filter_switch_sec`，换目标须覆盖 `slew_sec`，即
   `gap = max(min_readout_sec, 切换耗时)`。
4. 排程起点早于目标升起时，空闲等待至升起；前一目标超窗占用会向后顺延。
5. 同一目标名、同一目标内同一滤镜必须出现在连续块中，否则视为顺序歧义。

### 响应

**通过（HTTP 200）**：每帧起止时刻（含时区偏移）、帧前切换开销、按目标
的可见窗口、汇总统计与末帧状态。

```json
{
  "status": "accepted",
  "summary": {
    "total_frames": 11,
    "total_exposure_sec": 2820.0,
    "total_overhead_sec": 480.0,
    "overhead": {"readout_sec": 90.0, "filter_switch_sec": 270.0, "slew_sec": 120.0},
    "first_frame_start": "2026-10-06T20:53:26-03:00",
    "last_frame_end": "2026-10-06T21:48:26-03:00"
  },
  "targets": [
    {"index": 0, "name": "NGC253", "window_start": "...", "window_end": "...",
     "rise": "...", "set": "...", "transit": "...", "frames": 7,
     "window_remaining_sec": 30504.0}
  ],
  "frames": [
    {"seq": 0, "target": "NGC253", "target_index": 0, "filter": "g", "repeat": 1,
     "start": "2026-10-06T20:53:26-03:00", "end": "2026-10-06T20:58:26-03:00",
     "exposure_sec": 300.0, "gap_before_sec": 0.0,
     "overhead_before": {"type": "none", "seconds": 0.0,
       "readout_sec": 0.0, "filter_switch_sec": 0.0, "slew_sec": 0.0}}
  ],
  "last_frame": {
    "seq": 10, "target": "47Tuc", "filter": "i",
    "start": "...", "end": "2026-10-06T21:48:26-03:00",
    "status": "completed_within_window",
    "window_end": "2026-10-07T06:44:51-03:00",
    "window_remaining_sec": 32185.5
  }
}
```

**拒绝（HTTP 422）**：整批拒绝，`errors` 列出全部出错位置。

```json
{
  "status": "rejected",
  "errors": [
    {"position": "targets[0]", "code": "TARGET_NOT_VISIBLE",
     "message": "target 'M31' ... never reaches the minimum altitude of 30.0deg ..."},
    {"position": "targets[1].exposures[1]", "code": "FILTER_NOT_SUPPORTED",
     "message": "filter 'H-alpha' ... is not supported by the telescope ..."}
  ]
}
```

错误码：

| code | 含义 |
| ---- | ---- |
| `TARGET_NOT_VISIBLE` | 目标当夜升不到最低高度（位置：`targets[i]`） |
| `FILTER_NOT_SUPPORTED` | 滤镜超出望远镜能力（位置：`targets[i].exposures[j]`） |
| `WINDOW_EXCEEDED` | 序列总时长超出目标升落界（位置：`targets[i]`） |
| `ORDER_AMBIGUITY` | 目标/滤镜未连续成块，顺序含歧义（位置：再次出现处） |
| `INVALID_INPUT` | 请求体字段缺失或越界（位置：具体字段路径） |

非法 JSON 返回 HTTP 400（`INVALID_JSON`）。

## 调用示例

curl（仓库内置示例文件）：

```bash
# 通过：返回逐帧时刻表
curl -s -X POST http://localhost:8000/api/v1/schedule/audit \
  -H 'Content-Type: application/json' \
  -d @examples/accept.json | python3 -m json.tool

# 拒绝：整批拒绝并列出全部位置（HTTP 422）
curl -s -X POST http://localhost:8000/api/v1/schedule/audit \
  -H 'Content-Type: application/json' \
  -d @examples/reject.json | python3 -m json.tool
```

Python：

```python
import json, urllib.request

payload = json.load(open("examples/accept.json"))
req = urllib.request.Request(
    "http://localhost:8000/api/v1/schedule/audit",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req) as resp:
        result = json.load(resp)
except urllib.error.HTTPError as exc:      # 422: 整批拒绝
    result = json.load(exc)

if result["status"] == "accepted":
    for frame in result["frames"]:
        print(frame["seq"], frame["target"], frame["filter"],
              frame["start"], "->", frame["end"])
    print("last frame:", result["last_frame"]["status"])
else:
    for err in result["errors"]:
        print(err["position"], err["code"], err["message"])
```

## 本地开发与测试

无需安装依赖（Python ≥ 3.11）：

```bash
python -m app.main                          # 启动服务（默认 0.0.0.0:8000）
python -m unittest discover -s tests -v     # 运行测试（15 个用例）
```

## 项目结构

```
app/
  main.py        HTTP 服务（路由、JSON 解析、启动自检）
  validation.py  请求体校验，输出带位置的错误列表
  astro.py       恒星时与目标升/落/中天窗口计算
  scheduler.py   顺序排程与四类业务审计规则
tests/test_api.py  端到端测试（真实 HTTP 服务）
examples/          通过 / 拒绝两个示例请求
Dockerfile, docker-compose.yml, requirements.txt
```
