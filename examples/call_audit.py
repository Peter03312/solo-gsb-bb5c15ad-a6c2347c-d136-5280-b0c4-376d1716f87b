#!/usr/bin/env python3
"""排程审计 API 调用示例（标准库，无第三方依赖）。

用法：
    python examples/call_audit.py [BASE_URL]

默认 BASE_URL=http://127.0.0.1:8080
依次演示：成功审计、目标不可见拒绝、输入顺序歧义拒绝。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
HERE = Path(__file__).resolve().parent


def post(payload_file: str) -> dict:
    url = f"{BASE}/api/v1/audit"
    data = (HERE / payload_file).read_bytes()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def main() -> None:
    print(f"==> POST {BASE}/api/v1/audit  (success_m31.json)")
    ok = post("success_m31.json")
    print(f"accepted          : {ok['accepted']}")
    print(f"frames            : {ok['totals']['frame_count']}")
    print(f"first frame (UTC) : {ok['frames'][0]['start_utc']}")
    print(f"last frame (UTC)  : {ok['frames'][-1]['end_utc']}")
    print(f"readout total (s) : {ok['totals']['readout_seconds']}")
    print(f"filter change (s) : {ok['totals']['filter_change_seconds']}")
    print(f"last-frame status : {ok['last_frame']['status']} "
          f"(margin {ok['last_frame']['remaining_window_seconds']:.1f}s)")

    print("\n==> reject_not_visible.json")
    nv = post("reject_not_visible.json")
    print(json.dumps(nv, ensure_ascii=False, indent=2))

    print("\n==> reject_interleaved.json")
    iv = post("reject_interleaved.json")
    print(json.dumps(iv, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
