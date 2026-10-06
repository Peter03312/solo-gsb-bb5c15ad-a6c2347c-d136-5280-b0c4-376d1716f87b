"""End-to-end tests: run the real HTTP server on an ephemeral port."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

from app.main import AUDIT_PATH, AuditHandler


def _base_payload() -> dict:
    """A plan that is accepted: target culminates high at a southern site."""
    return {
        "observation_date": "2026-10-06",
        "timezone": "UTC",
        "site": {"latitude_deg": -30.0, "longitude_deg": -70.0,
                 "min_altitude_deg": 0.0},
        "targets": [
            {
                "name": "T1",
                "ra_deg": 305.0,
                "dec_deg": -30.0,
                "exposures": [
                    {"filter": "g", "exposure_sec": 300, "count": 2},
                    {"filter": "r", "exposure_sec": 200, "count": 1},
                ],
            }
        ],
        "telescope": {
            "supported_filters": ["g", "r", "i"],
            "filter_switch_sec": 60,
            "min_readout_sec": 10,
            "slew_sec": 45,
        },
    }


class ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AuditHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post(self, path: str, body) -> tuple:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _get(self, path: str) -> tuple:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


class TestSmoke(ServerFixture):
    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_root_info(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("audit", body["endpoints"])

    def test_not_found(self):
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)


class TestAcceptance(ServerFixture):
    def test_accept_single_target(self):
        status, body = self._post(AUDIT_PATH, _base_payload())
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "accepted")

        frames = body["frames"]
        self.assertEqual(len(frames), 3)  # 2 x g + 1 x r

        # Frames are sequential and every gap honours the 10 s readout.
        for prev, cur in zip(frames, frames[1:]):
            self.assertEqual(prev["end"], _minus(cur["start"],
                                                 cur["gap_before_sec"]))
            self.assertGreaterEqual(cur["gap_before_sec"], 10)

        # g -> g is pure readout; g -> r pays the 60 s filter switch.
        self.assertEqual(frames[1]["overhead_before"]["type"], "readout")
        self.assertEqual(frames[1]["gap_before_sec"], 10)
        self.assertEqual(frames[2]["overhead_before"]["type"], "filter_switch")
        self.assertEqual(frames[2]["gap_before_sec"], 60)

        # Repeats of one entry are consecutive and numbered from 1.
        self.assertEqual([f["repeat"] for f in frames], [1, 2, 1])
        self.assertEqual([f["filter"] for f in frames], ["g", "g", "r"])

        # Summary and last-frame status.
        summary = body["summary"]
        self.assertEqual(summary["total_frames"], 3)
        self.assertEqual(summary["total_exposure_sec"], 800)
        self.assertEqual(summary["overhead"]["filter_switch_sec"], 60)
        self.assertEqual(summary["overhead"]["readout_sec"], 10)
        last = body["last_frame"]
        self.assertEqual(last["status"], "completed_within_window")
        self.assertEqual(last["filter"], "r")
        self.assertGreaterEqual(last["window_remaining_sec"], 0)

        # Every frame lies inside its target's rise/set window.
        window = body["targets"][0]
        for frame in frames:
            self.assertGreaterEqual(frame["start"], window["window_start"])
            self.assertLessEqual(frame["end"], window["window_end"])

    def test_accept_multi_target_with_slew(self):
        payload = _base_payload()
        payload["targets"].append({
            "name": "T2",
            "ra_deg": 320.0,
            "dec_deg": -40.0,
            "exposures": [{"filter": "i", "exposure_sec": 100, "count": 1}],
        })
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 200, body)
        frames = body["frames"]
        self.assertEqual(len(frames), 4)
        # Target change charges max(readout, slew) = 45 s.
        self.assertEqual(frames[3]["overhead_before"]["type"], "slew")
        self.assertEqual(frames[3]["gap_before_sec"], 45)
        self.assertEqual(body["summary"]["overhead"]["slew_sec"], 45)

    def test_schedule_start_is_respected(self):
        payload = _base_payload()
        payload["schedule_start"] = "2026-10-06T23:00:00"  # naive => site tz
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 200, body)
        window_start = body["targets"][0]["window_start"]
        expected = max("2026-10-06T23:00:00+00:00", window_start)
        self.assertEqual(body["frames"][0]["start"], expected)
        self.assertEqual(body["frames"][0]["gap_before_sec"], 0)


class TestRejections(ServerFixture):
    def test_filter_not_supported(self):
        payload = _base_payload()
        payload["targets"][0]["exposures"][1]["filter"] = "H-alpha"
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 422)
        self.assertEqual(body["status"], "rejected")
        err = body["errors"][0]
        self.assertEqual(err["code"], "FILTER_NOT_SUPPORTED")
        self.assertEqual(err["position"], "targets[0].exposures[1]")

    def test_target_not_visible(self):
        payload = _base_payload()
        payload["targets"][0]["dec_deg"] = 80.0  # never rises at lat -30
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 422)
        err = body["errors"][0]
        self.assertEqual(err["code"], "TARGET_NOT_VISIBLE")
        self.assertEqual(err["position"], "targets[0]")

    def test_window_exceeded(self):
        payload = _base_payload()
        payload["targets"][0]["exposures"] = [
            {"filter": "g", "exposure_sec": 3600, "count": 20},
        ]
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 422)
        err = body["errors"][0]
        self.assertEqual(err["code"], "WINDOW_EXCEEDED")
        self.assertEqual(err["position"], "targets[0]")

    def test_order_ambiguity_target_blocks(self):
        payload = _base_payload()
        second = {"name": "T2", "ra_deg": 310.0, "dec_deg": -20.0,
                  "exposures": [{"filter": "g", "exposure_sec": 60,
                                 "count": 1}]}
        payload["targets"] = [payload["targets"][0], second,
                              dict(payload["targets"][0])]
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 422)
        codes = [e["code"] for e in body["errors"]]
        self.assertIn("ORDER_AMBIGUITY", codes)
        err = body["errors"][codes.index("ORDER_AMBIGUITY")]
        self.assertEqual(err["position"], "targets[2]")

    def test_order_ambiguity_filter_blocks(self):
        payload = _base_payload()
        payload["targets"][0]["exposures"] = [
            {"filter": "g", "exposure_sec": 60, "count": 1},
            {"filter": "r", "exposure_sec": 60, "count": 1},
            {"filter": "g", "exposure_sec": 60, "count": 1},
        ]
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 422)
        err = body["errors"][0]
        self.assertEqual(err["code"], "ORDER_AMBIGUITY")
        self.assertEqual(err["position"], "targets[0].exposures[2]")

    def test_multiple_errors_are_all_listed(self):
        payload = _base_payload()
        payload["targets"][0]["dec_deg"] = 80.0          # not visible
        payload["targets"][0]["exposures"][0]["filter"] = "X"  # unsupported
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 422)
        codes = sorted(e["code"] for e in body["errors"])
        self.assertEqual(codes, ["FILTER_NOT_SUPPORTED", "TARGET_NOT_VISIBLE"])

    def test_invalid_schema_positions(self):
        payload = _base_payload()
        payload["observation_date"] = "2026-13-40"
        payload["timezone"] = "Mars/Olympus"
        payload["targets"][0]["exposures"][0]["count"] = 0
        payload["targets"][0]["ra_deg"] = 360.0
        status, body = self._post(AUDIT_PATH, payload)
        self.assertEqual(status, 422)
        positions = {e["position"] for e in body["errors"]}
        self.assertIn("$.observation_date", positions)
        self.assertIn("$.timezone", positions)
        self.assertIn("targets[0].exposures[0].count", positions)
        self.assertIn("targets[0].ra_deg", positions)
        self.assertTrue(all(e["code"] == "INVALID_INPUT"
                            for e in body["errors"]))

    def test_invalid_json(self):
        status, body = self._post(AUDIT_PATH, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["errors"][0]["code"], "INVALID_JSON")

    def test_missing_fields(self):
        status, body = self._post(AUDIT_PATH, {"observation_date":
                                               "2026-10-06"})
        self.assertEqual(status, 422)
        self.assertGreaterEqual(len(body["errors"]), 4)


def _minus(iso_ts: str, seconds: float) -> str:
    dt = datetime.fromisoformat(iso_ts)
    from datetime import timedelta
    return (dt - timedelta(seconds=seconds)).isoformat()


if __name__ == "__main__":
    unittest.main(verbosity=2)
