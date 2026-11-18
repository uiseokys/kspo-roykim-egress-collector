"""Merge raw artifacts by data timestamp, then produce coverage and curves."""

import argparse
import json
from datetime import timedelta
from pathlib import Path

from collector import PARK, SEGMENTS, STEP, clock
from plot_roykim_egress import SHOW_STARTS, save_figure, save_table


def report(inputs, output):
    by_show = {show.date().isoformat(): {} for show in SHOW_STARTS}
    polls = {day: set() for day in by_show}
    for path in inputs:
        with path.open(encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                row = json.loads(line)
                day = row["show_day"]
                if row["AREA_NM"] != PARK or day not in by_show:
                    raise ValueError("수집 대상과 일치하지 않는 관측입니다.")
                received = clock(row["received_at_KST"])
                stamp = clock(row["PPLTN_TIME"])
                range_ = (row["AREA_PPLTN_MIN"], row["AREA_PPLTN_MAX"])
                polls[day].add(clock(row["poll_slot_KST"]))
                previous = by_show[day].get(stamp)
                if previous is None or received > previous[0]:
                    by_show[day][stamp] = received, range_

    output.mkdir(parents=True, exist_ok=True)
    curves = {}
    lines = ["# 로이킴 회차별 수집 범위", "",
             "시간은 KST이며 인구는 올림픽공원 전체 추정치입니다.", "",
             "| 회차 | 요청 성공 슬롯 / 계획 슬롯 | 고유 기준시각 | 수집 범위 내 첫 기준시각 | 마지막 기준시각 |",
             "|---|---:|---:|---|---|"]
    for show in SHOW_STARTS:
        day = show.date().isoformat()
        windows = [segment for segment in SEGMENTS.values() if segment.show_day == day]
        first = min(segment.first for segment in windows)
        last = max(segment.last for segment in windows)
        recorded = sorted((stamp, bounds) for stamp, (_, bounds) in by_show[day].items() if first <= stamp < last)
        curves[show] = recorded
        captured = sum(first <= slot < last for slot in polls[day])
        planned = (last - first) // STEP
        observed = f"{recorded[0][0]:%m/%d %H:%M} | {recorded[-1][0]:%m/%d %H:%M}" if recorded else "미확인 | 미확인"
        lines.append(f"| {day} | {captured}/{planned} | {len(recorded)} | {observed} |")
    lines.extend(["",
                  "요청 시간과 자료 기준시각은 다를 수 있습니다. 같은 기준시각의 관측은 마지막 조회값을 사용했습니다.",
                  "회차별 실제 종료 시각은 자료에 포함되어 있지 않습니다."])
    summary = "\n".join(lines) + "\n"
    (output / "수집범위.md").write_text(summary, encoding="utf-8")
    print(summary, end="")
    if any(curves.values()):
        save_table(output / "로이킴_회차별_실시간인구.csv", curves, {})
        save_figure(output / "로이킴_회차별_공원인구_퇴장곡선.png", curves, {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    files = sorted({path for directory in args.input_dir for path in directory.rglob("*.jsonl")})
    report(files, args.output)
