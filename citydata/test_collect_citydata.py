"""전체 항목 수집기 시험. 실행: python3 -m unittest citydata/test_collect_citydata.py"""

import gzip
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_citydata as cc  # noqa: E402

HERE = Path(__file__).resolve().parent
SAMPLE = json.loads((HERE / "sample_citydata.json").read_text(encoding="utf-8"))  # 공개 시험 키로 받은 응답을 줄인 것(광화문·덕수궁)
WORKFLOW = (HERE.parent / ".github/workflows/collect-citydata.yml").read_text(encoding="utf-8")
CITY = SAMPLE["CITYDATA"]


def kst(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=cc.KST)


class ToRowTest(unittest.TestCase):
    def test_sample_response(self):
        row = cc.to_row(SAMPLE, cc.now_kst())
        people, sub, bus = CITY["LIVE_PPLTN_STTS"][0], CITY["LIVE_SUB_PPLTN"], CITY["LIVE_BUS_PPLTN"]
        self.assertEqual(row["status"], "ok")
        self.assertEqual((row["ppltn_time"], row["ppltn_min"], row["rate_50"]), (people["PPLTN_TIME"], people["AREA_PPLTN_MIN"], people["PPLTN_RATE_50"]))
        self.assertEqual((row["sub_on_5_min"], row["sub_off_30_max"]), (sub["SUB_5WTHN_GTON_PPLTN_MIN"], sub["SUB_30WTHN_GTOFF_PPLTN_MAX"]))
        self.assertEqual((row["sub_on_acml_max"], row["sub_stn_cnt"]), (sub["SUB_ACML_GTON_PPLTN_MAX"], sub["SUB_STN_CNT"]))
        self.assertEqual(row["bus_on_10_min"], bus["BUS_10WTHN_GTON_PPLTN_MIN"])
        live = [p for p in CITY["PRK_STTS"] if p["CUR_PRK_YN"] == "Y"]
        self.assertTrue(0 < len(live) < len(CITY["PRK_STTS"]))  # 실시간이 아닌 주차장은 더하지 않음
        self.assertEqual((row["prk_lots"], row["prk_current"]), (len(live), sum(int(p["CUR_PRK_CNT"]) for p in live)))
        self.assertEqual(row["bike_parked"], sum(int(b["SBIKE_PARKING_CNT"]) for b in CITY["SBIKE_STTS"]))
        self.assertEqual(row["road_spd"], CITY["ROAD_TRAFFIC_STTS"]["AVG_ROAD_DATA"]["ROAD_TRAFFIC_SPD"])
        self.assertEqual(set(row), set(cc.FIELDS))

    def test_empty_or_partial_response(self):
        self.assertEqual(cc.to_row({}, cc.now_kst())["status"], "empty")
        partial = {"CITYDATA": {"LIVE_SUB_PPLTN": CITY["LIVE_SUB_PPLTN"], "PRK_STTS": None, "ROAD_TRAFFIC_STTS": []}}
        row = cc.to_row(partial, cc.now_kst())
        self.assertEqual((row["status"], row["sub_on_5_min"], row["prk_lots"]), ("empty", CITY["LIVE_SUB_PPLTN"]["SUB_5WTHN_GTON_PPLTN_MIN"], ""))

    def test_trim_keeps_only_listed_sections_and_columns(self):
        full = {"CITYDATA": {**CITY, "EVENT_STTS": [{"EVENT_NM": "x" * 500}], "CHARGER_STTS": [{"STAT_NM": "y"}]}}
        small = cc.trim(full)
        self.assertEqual(set(small) - {"AREA_NM", "AREA_CD"}, {k for k in cc.KEEP if CITY.get(k) not in (None, [], {})})
        self.assertNotIn("FCST_PPLTN", small["LIVE_PPLTN_STTS"][0])
        self.assertEqual(set(small["PRK_STTS"][0]), set(cc.KEEP["PRK_STTS"]))


class PlanTest(unittest.TestCase):
    def test_segments_start_one_hour_before_show_and_fit_one_job(self):
        segs = cc.segments()
        self.assertEqual(segs["1127_조용필-A"][0], kst("2026-11-27 18:00"))  # 19:00 공연의 1시간 전
        self.assertEqual(segs["1128_조용필-A"][0], kst("2026-11-28 17:00"))
        self.assertEqual(segs["1129_조용필-A"][0], kst("2026-11-29 16:00"))
        self.assertEqual(segs["1127_조용필-B"][1], kst("2026-11-28 01:00"))
        timeout = int(re.search(r"timeout-minutes: (\d+)\n    concurrency", WORKFLOW).group(1))
        for name, (first, last, _) in segs.items():
            longest = (last - (first - cc.PLAN_EARLY)).total_seconds() / 60
            self.assertLess(longest + 10, timeout, name)  # 가장 일찍 시작해도 작업 제한 시간 안
        for label in cc.SHOWS:  # 두 구간이 겹쳐 빈틈이 없음
            self.assertGreater(segs[f"{label}-A"][1], segs[f"{label}-B"][0])

    def test_plan_by_time(self):
        self.assertEqual(cc.plan("", kst("2026-11-27 17:00")), ("idle", ""))  # 아직 이름
        self.assertEqual(cc.plan("", kst("2026-11-27 17:20")), ("collect", "1127_조용필-A"))  # 1시간 전보다 조금 일찍 와도 시작
        self.assertEqual(cc.plan("", kst("2026-11-27 20:00")), ("collect", "1127_조용필-A"))
        self.assertEqual(cc.plan("", kst("2026-11-27 21:00")), ("collect", "1127_조용필-B"))  # 겹치는 때는 늦게 시작하는 구간
        self.assertEqual(cc.plan("", kst("2026-11-28 00:59")), ("collect", "1127_조용필-B"))
        self.assertEqual(cc.plan("", kst("2026-11-28 01:00")), ("idle", ""))
        self.assertEqual(cc.plan("auto", kst("2026-11-29 16:30")), ("collect", "1129_조용필-A"))
        self.assertEqual(cc.plan("check", kst("2026-11-27 20:00")), ("check", ""))
        self.assertEqual(cc.plan("test", kst("2026-11-26 15:00")), ("test", ""))

    def test_crons_cover_every_window_even_if_eight_hours_late(self):
        crons = re.findall(r'cron: "([\d,]+) ([\d\-*]+) ([\d\-]+) 11 \*"', WORKFLOW)
        fires = []
        for minutes, hours, days in crons:
            hour_list = range(24) if hours == "*" else range(int(hours.split("-")[0]), int(hours.split("-")[1]) + 1)
            day_list = range(int(days.split("-")[0]), int(days.split("-")[-1]) + 1)
            fires += [datetime(2026, 11, d, h, int(m), tzinfo=cc.KST) + timedelta(hours=9) for d in day_list for h in hour_list for m in minutes.split(",")]
        for delay_hours in (0, 4, 8):  # 로이킴 때는 7~8시간 늦게 돌았음
            actual = [f + timedelta(hours=delay_hours) for f in fires]
            for name, (first, last, _) in cc.segments().items():
                started = [t for t in actual if first - cc.PLAN_EARLY <= t <= first and cc.plan("", t) == ("collect", name)]
                self.assertTrue(started, f"{name}: {delay_hours}시간 늦을 때 구간 시작 전에 도는 예약이 없음")


class RunTest(unittest.TestCase):
    def test_run_writes_files_and_survives_errors(self):
        calls = {"n": 0}

        def fake_fetch(key):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("HTTP 500")
            return SAMPLE

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cc, "OUT_DIR", Path(tmp) / "data"), mock.patch.object(cc, "ROOT", Path(tmp)), \
                mock.patch.object(cc, "fetch", side_effect=fake_fetch), mock.patch.object(cc.time, "sleep"):
            start = cc.now_kst()
            ticks = iter(start + timedelta(seconds=5 * i) for i in range(1000))
            with mock.patch.object(cc, "now_kst", side_effect=lambda: next(ticks)):
                code = cc.run("secret-key", "t", "A-1", start + timedelta(minutes=2), 120, full_dir=Path(tmp) / "full")
            self.assertEqual(code, 0)
            body = (Path(tmp) / "data/t/A-1.csv").read_text(encoding="utf-8")
            self.assertGreaterEqual(len(body.splitlines()), 3)
            self.assertIn("error: RuntimeError", body)
            self.assertNotIn("secret-key", body)
            kept = [json.loads(line) for line in (Path(tmp) / "data/t/A-1.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(kept and "LIVE_SUB_PPLTN" in kept[0]["response"] and "EVENT_STTS" not in kept[0]["response"])
            with gzip.open(Path(tmp) / "full/t-A-1.jsonl.gz", "rt", encoding="utf-8") as f:
                self.assertIn("CITYDATA", json.loads(f.readline())["response"])

    def test_empty_response_cannot_count_as_success(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cc, "OUT_DIR", Path(tmp)), mock.patch.object(cc, "ROOT", Path(tmp)), \
                mock.patch.object(cc, "fetch", return_value={}), mock.patch.object(cc.time, "sleep"):
            start = cc.now_kst()
            ticks = (start + timedelta(seconds=i) for i in range(300))
            with mock.patch.object(cc, "now_kst", side_effect=lambda: next(ticks)):
                self.assertEqual(cc.run("fixture", "test", "x", start + timedelta(seconds=20), 5), 1)

    def test_fetch_error_never_carries_the_key(self):
        with mock.patch.object(cc.urllib.request, "urlopen", side_effect=cc.urllib.error.URLError("http://host/secret-key/json")), mock.patch.object(cc.time, "sleep"):
            with self.assertRaises(RuntimeError) as caught:
                cc.fetch("secret-key")
        self.assertNotIn("secret-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
