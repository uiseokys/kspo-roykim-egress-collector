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

    def test_segments_leave_no_gap_from_start_to_end(self):
        # 11/27 14:00부터 11/29 23:00까지: 시간 순서로 놓으면 다음 구간이 앞 구간이 끝나기 전에 시작함
        spans = list(cc.segments().values())
        self.assertEqual(spans, sorted(spans, key=lambda span: span[0]))
        self.assertEqual((spans[0][0], spans[-1][1]), (kst("2026-11-27 14:00"), kst("2026-11-29 23:00")))
        for (_, last, _), (first, _, _) in zip(spans, spans[1:]):
            self.assertLess(first, last)
        now = kst("2026-11-27 14:00")
        while now < kst("2026-11-29 23:00"):  # 어느 시각에 실행돼도 수집할 구간이 있음
            self.assertEqual(cc.plan("", now)[0], "collect", now)
            now += timedelta(minutes=7)

    def test_plan_by_time(self):
        self.assertEqual(cc.plan("", kst("2026-11-27 13:00")), ("idle", ""))  # 아직 이름
        self.assertEqual(cc.plan("", kst("2026-11-27 14:20")), ("collect", "1127_조용필-P1"))  # 공연 구간 앞의 빈 시간
        self.assertEqual(cc.plan("", kst("2026-11-27 17:20")), ("collect", "1127_조용필-A"))  # 1시간 전보다 조금 일찍 와도 시작
        self.assertEqual(cc.plan("", kst("2026-11-27 20:00")), ("collect", "1127_조용필-A"))
        self.assertEqual(cc.plan("", kst("2026-11-27 21:00")), ("collect", "1127_조용필-B"))  # 겹치는 때는 늦게 시작하는 구간
        self.assertEqual(cc.plan("", kst("2026-11-28 00:05")), ("collect", "1127_조용필-B"))
        self.assertEqual(cc.plan("", kst("2026-11-28 01:00")), ("collect", "1128_조용필-P1"))  # 공연이 끝난 밤도 이어서
        self.assertEqual(cc.plan("arm", kst("2026-11-28 10:00")), ("collect", "1128_조용필-P3"))
        self.assertEqual(cc.plan("auto", kst("2026-11-29 16:30")), ("collect", "1129_조용필-A"))
        self.assertEqual(cc.plan("check", kst("2026-11-27 20:00")), ("check", ""))
        # arm: 시작 전이면 기다리고, 그 뒤로는 바로 수집, 마지막 구간이 끝난 뒤에는 멈춤
        self.assertEqual(cc.plan("arm", kst("2026-11-26 21:00")), ("wait", ""))
        self.assertEqual(cc.plan("arm", kst("2026-11-27 17:30")), ("collect", "1127_조용필-A"))
        self.assertEqual(cc.plan("arm", kst("2026-11-29 23:30")), ("idle", ""))
        self.assertEqual(cc.plan("", kst("2026-11-26 21:00")), ("idle", ""))  # 예약 실행은 기다리지 않음

    def test_handoff_keeps_everything_covered_with_one_start(self):
        # 모든 구간은 다음 구간 시작 30분 전에 다음 실행을 요청하고, 그때 고르는 구간이 바로 다음 구간이어야 함
        names = list(cc.segments())
        for name, following in zip(names, names[1:]):
            during, after = cc.handoff_for(name, relay=False)
            self.assertEqual((during[1], after), ("auto", None), name)
            self.assertEqual(cc.plan(during[1], during[0] + timedelta(minutes=3)), ("collect", following))
            self.assertLess(during[0], cc.segments()[name][1])
        self.assertEqual(cc.handoff_for("1127_조용필-P1", relay=False)[0][0], kst("2026-11-27 17:30"))
        self.assertEqual(cc.handoff_for("1127_조용필-A", relay=False)[0][0], kst("2026-11-27 21:00"))
        self.assertEqual(cc.handoff_for("1127_조용필-B", relay=False)[0][0], kst("2026-11-28 00:25"))
        self.assertEqual(cc.handoff_for("1127_조용필-A", relay=True)[0][1], "arm")
        self.assertEqual(cc.handoff_for("1129_조용필-B", relay=True), (None, None))  # 마지막 구간에서 멈춤

    def test_one_start_now_runs_to_the_end_without_a_gap(self):
        # 14:20에 한 번 시작: 각 실행은 시작한 때부터 구간 끝까지 모으고, 다음 실행은 앞 실행이 끝나기 전에 시작함
        now, covered_until, picked = kst("2026-11-27 14:20"), None, []
        while True:
            mode, name = cc.plan("auto", now)
            if mode != "collect":
                break
            if covered_until is not None:
                self.assertLess(now, covered_until, name)
            picked.append(name)
            covered_until = cc.segments()[name][1]
            during, _ = cc.handoff_for(name, relay=False)
            if during is None:
                break
            now = during[0] + timedelta(minutes=2)  # 요청한 실행이 2분 뒤에 시작
        self.assertEqual(picked, list(cc.segments()))
        self.assertEqual(covered_until, kst("2026-11-29 23:00"))

    def test_scheduled_run_rests_while_another_run_is_active(self):
        listing = json.dumps([{"databaseId": 1, "status": "completed"}, {"databaseId": 7, "status": "in_progress"}, {"databaseId": 9, "status": "in_progress"}]).encode()
        with mock.patch.dict(cc.os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "9"}), mock.patch.object(cc.subprocess, "run") as called:
            called.return_value = mock.Mock(stdout=listing)
            self.assertTrue(cc.others_active())  # 7번이 돌고 있음
            called.return_value = mock.Mock(stdout=json.dumps([{"databaseId": 9, "status": "in_progress"}]).encode())
            self.assertFalse(cc.others_active())  # 나 혼자뿐
            called.return_value = mock.Mock(stdout=b"")
            self.assertFalse(cc.others_active())  # 확인하지 못하면 수집하는 쪽으로
        with mock.patch.dict(cc.os.environ, {"GITHUB_ACTIONS": ""}):
            self.assertFalse(cc.others_active())

    def test_wait_chain_reaches_every_window(self):
        # arm을 전날 밤에 눌렀을 때: 5시간씩 기다리며 이어 가도 각 구간이 시작하기 전에 수집을 고름
        now, picked = kst("2026-11-26 21:10"), []
        for _ in range(60):
            mode, name = cc.plan("arm", now)
            if mode == "wait":
                now += min(cc.upcoming(now) - cc.PLAN_EARLY + timedelta(minutes=1) - now, cc.RELAY_MAX) + timedelta(minutes=2)
            elif mode == "collect":
                picked.append((name, now <= cc.segments()[name][0]))
                during, after = cc.handoff_for(name, relay=True)
                now = during[0] + timedelta(minutes=2) if during else cc.segments()[name][1] + timedelta(minutes=2)
            else:
                break
        self.assertEqual([p[0] for p in picked], list(cc.segments()))
        self.assertTrue(all(early for name, early in picked if name.endswith("-A")))  # 앞 구간은 공연 1시간 전보다 먼저 시작
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

        def fake_fetch(key, area=cc.AREA, **options):
            if area != cc.AREA:  # 함께 모으는 장소
                calls["extra"] = calls.get("extra", 0) + 1
                return SAMPLE
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("HTTP 500")
            return SAMPLE

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cc, "OUT_DIR", Path(tmp) / "data"), mock.patch.object(cc, "ROOT", Path(tmp)), \
                mock.patch.object(cc, "fetch", side_effect=fake_fetch), mock.patch.object(cc.time, "sleep"):
            start = cc.now_kst()
            ticks = iter(start + timedelta(seconds=5 * i) for i in range(1000))
            with mock.patch.object(cc, "now_kst", side_effect=lambda: next(ticks)):
                with mock.patch.object(cc, "dispatch") as asked:
                    code = cc.run("secret-key", "t", "A-1", start + timedelta(minutes=2), 120, full_dir=Path(tmp) / "full",
                                  handoff=(start + timedelta(seconds=30), "auto"))
            self.assertEqual(code, 0)
            asked.assert_called_once_with("auto")  # 정한 시각이 지나면 다음 실행을 한 번만 요청
            body = (Path(tmp) / "data/t/A-1.csv").read_text(encoding="utf-8")
            self.assertGreaterEqual(len(body.splitlines()), 3)
            self.assertIn("error: RuntimeError", body)
            self.assertNotIn("secret-key", body)
            kept = [json.loads(line) for line in (Path(tmp) / "data/t/A-1.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(kept and "LIVE_SUB_PPLTN" in kept[0]["response"] and "EVENT_STTS" not in kept[0]["response"])
            with gzip.open(Path(tmp) / "full/t-A-1.jsonl.gz", "rt", encoding="utf-8") as f:
                self.assertIn("CITYDATA", json.loads(f.readline())["response"])
            # 함께 모으는 장소(잠실 관광특구)는 따로 쌓고, 추린 원본에는 인구·지하철·버스만
            extra_rows = (Path(tmp) / "data/t/A-1.jamsil.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(extra_rows) - 1, calls["extra"])
            self.assertLess(calls["extra"], calls["n"])  # 5분 간격이라 올림픽공원보다 덜 자주
            extra = json.loads((Path(tmp) / "data/t/A-1.jamsil.jsonl").read_text(encoding="utf-8").splitlines()[0])["response"]
            self.assertTrue("LIVE_SUB_PPLTN" in extra and "SBIKE_STTS" not in extra and "PRK_STTS" not in extra)

    def test_extra_area_failure_never_stops_the_main_collection(self):
        def fake_fetch(key, area=cc.AREA, **options):
            if area != cc.AREA:
                raise RuntimeError("HTTP 500")
            return SAMPLE

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cc, "OUT_DIR", Path(tmp)), mock.patch.object(cc, "ROOT", Path(tmp)), \
                mock.patch.object(cc, "fetch", side_effect=fake_fetch), mock.patch.object(cc.time, "sleep"):
            start = cc.now_kst()
            ticks = (start + timedelta(seconds=5 * i) for i in range(1000))
            with mock.patch.object(cc, "now_kst", side_effect=lambda: next(ticks)):
                self.assertEqual(cc.run("secret-key", "t", "x", start + timedelta(minutes=2), 30), 0)
            self.assertNotIn("error", (Path(tmp) / "t/x.csv").read_text(encoding="utf-8"))
            body = (Path(tmp) / "t/x.jamsil.csv").read_text(encoding="utf-8")
            self.assertIn("error: RuntimeError", body)
            self.assertNotIn("secret-key", body)
            self.assertFalse((Path(tmp) / "t/x.jamsil.jsonl").exists())

    def test_extra_only_run_collects_just_the_extra_area(self):
        self.assertEqual(cc.plan("extra", kst("2026-11-27 15:00")), ("extra", ""))
        seen = []

        def fake_fetch(key, area=cc.AREA, **options):
            seen.append(area)
            return SAMPLE

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cc, "OUT_DIR", Path(tmp)), mock.patch.object(cc, "ROOT", Path(tmp)), \
                mock.patch.object(cc, "fetch", side_effect=fake_fetch), mock.patch.object(cc.time, "sleep"), mock.patch.object(cc, "save_to_repo", return_value=True) as saving:
            start = cc.now_kst()
            ticks = (start + timedelta(minutes=2 * i) for i in range(1000))
            with mock.patch.object(cc, "now_kst", side_effect=lambda: next(ticks)):
                self.assertEqual(cc.run_extra("secret-key", "t", "P1-9", start + timedelta(minutes=40), push=True), 0)
            self.assertEqual(set(seen), set(cc.EXTRA_AREAS))  # 올림픽공원은 조회하지 않음
            self.assertEqual(len((Path(tmp) / "t/P1-9.jamsil.csv").read_text(encoding="utf-8").splitlines()) - 1, len(seen))
            self.assertFalse((Path(tmp) / "t/P1-9.csv").exists())
            self.assertIn("진행 중", saving.call_args_list[0].args[1])  # 첫 조회 직후 바로 저장
            self.assertGreaterEqual(saving.call_count, 2)

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
