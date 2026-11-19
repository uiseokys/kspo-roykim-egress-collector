"""Fetch only Olympic Park aggregate population readings during the Roy Kim shows."""

import argparse
import http.client
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
STEP = timedelta(minutes=5)
HOST = "openapi.seoul.go.kr"
PARK = "올림픽공원"
FIELDS = ("PPLTN_TIME", "AREA_PPLTN_MIN", "AREA_PPLTN_MAX")
REQUEST_TIMEOUT = 25
RETRY_DELAYS = (0, 10, 20, 30)


@dataclass(frozen=True)
class Segment:
    first: datetime
    last: datetime
    show_day: str
    final: bool


def clock(value):
    time_value = datetime.fromisoformat(value)
    return time_value.replace(tzinfo=KST) if time_value.tzinfo is None else time_value.astimezone(KST)


def segment(first, last, final=False):
    return Segment(clock(first), clock(last), first[:10], final)


SEGMENTS = {
    "nov20-early": segment("2026-11-20 18:30", "2026-11-20 22:15"),
    "nov20-late": segment("2026-11-20 22:10", "2026-11-21 02:00", True),
    "nov21-early": segment("2026-11-21 16:30", "2026-11-21 20:15"),
    "nov21-late": segment("2026-11-21 20:10", "2026-11-22 00:00", True),
    "nov22-early": segment("2026-11-22 15:30", "2026-11-22 19:15"),
    "nov22-late": segment("2026-11-22 19:10", "2026-11-22 23:00", True),
}
CRON_SEGMENT = dict(zip(
    ("13 18 20 11 *", "56 21 20 11 *", "13 16 21 11 *",
     "56 19 21 11 *", "13 15 22 11 *", "56 18 22 11 *"),
    SEGMENTS,
))


def plan(request, schedule, now):
    chosen = CRON_SEGMENT.get(schedule, request)
    if chosen in {"check", "sample", "report"}:
        return chosen, ""
    selected = SEGMENTS.get(chosen)
    if selected is None or now < selected.first - timedelta(hours=1):
        return "skip", ""
    if now >= selected.last:
        recovery_limit = selected.last + timedelta(hours=12)
        return ("report", "") if selected.final and now < recovery_limit else ("skip", "")
    return ("collect-report" if selected.final else "collect"), chosen


def api_rows(value):
    if isinstance(value, list):
        for row in value:
            yield from api_rows(row)
    elif isinstance(value, dict):
        if all(field in value for field in FIELDS):
            yield value
        else:
            for nested in value.values():
                yield from api_rows(nested)


def query(key, place):
    route = f"/{quote(key, safe='')}/json/citydata_ppltn/1/5/{quote(place, safe='')}"
    connection = http.client.HTTPConnection(HOST, 8088, timeout=REQUEST_TIMEOUT)
    try:
        connection.request("GET", route, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        payload = json.loads(response.read(2_000_000))
    finally:
        connection.close()
    rows = [row for row in api_rows(payload) if str(row.get("AREA_NM") or "").strip() == place]
    if len(rows) != 1:
        result = payload.get("RESULT", {}) if isinstance(payload, dict) else {}
        code = result.get("CODE", "") if isinstance(result, dict) else ""
        detail = code if isinstance(code, str) and code.startswith(("ERROR-", "INFO-")) and code.split("-")[-1].isdigit() else ""
        raise ValueError(f"인구 관측을 찾지 못했습니다. {detail}".strip())
    row = rows[0]
    now = datetime.now(KST)
    stamp = clock(row["PPLTN_TIME"])
    lower, upper = (int(str(row[field]).replace(",", "")) for field in FIELDS[1:])
    if not (0 <= lower <= upper) or stamp > now + STEP:
        raise ValueError("인구 범위 또는 자료 기준시각 오류")
    return {
        "AREA_NM": place,
        "PPLTN_TIME": stamp.isoformat(),
        "AREA_PPLTN_MIN": lower,
        "AREA_PPLTN_MAX": upper,
        "received_at_KST": now.isoformat(),
    }


def required_key():
    value = os.getenv("SEOUL_POPULATION_API_KEY", "").strip()
    if not value:
        raise SystemExit("저장소 Actions Secrets에 SEOUL_POPULATION_API_KEY를 등록하세요.")
    return value


def safe_query(key, place):
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            return query(key, place)
        except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
            # Exception bodies and request URLs can contain the key.
            print(f"서울시 API 실패 {attempt}/{len(RETRY_DELAYS)} ({type(error).__name__})", flush=True)
    return None


def collect(name, output):
    selected = SEGMENTS[name]
    now = datetime.now(KST)
    if now < selected.first - timedelta(hours=1) or now >= selected.last:
        raise SystemExit("이 구간의 수집 시간이 아닙니다.")
    key = required_key()
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{name}.jsonl"
    print(f"{name} · {selected.first.isoformat()} ~ {selected.last.isoformat()}", flush=True)
    time.sleep(max(0, (selected.first - now).total_seconds()))
    while (now := datetime.now(KST)) < selected.last:
        slot = selected.first + ((now - selected.first) // STEP) * STEP
        record = safe_query(key, PARK)
        if record:
            record["show_day"] = selected.show_day
            record["poll_slot_KST"] = slot.isoformat()
            with path.open("a", encoding="utf-8") as source:
                source.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{slot:%H:%M} 저장 · 자료 기준 {record['PPLTN_TIME']}", flush=True)
        next_poll = min(slot + STEP, selected.last)
        time.sleep(max(0, (next_poll - datetime.now(KST)).total_seconds()))
    if not path.exists():
        raise SystemExit("수집 구간에서 저장된 관측이 없습니다.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "collect", "check", "sample"))
    parser.add_argument("--segment", choices=SEGMENTS)
    parser.add_argument("--output", type=Path, default=Path("raw"))
    args = parser.parse_args()
    if args.action == "plan":
        mode, chosen = plan(os.getenv("RUN_REQUEST", ""), os.getenv("RUN_CRON", ""), datetime.now(KST))
        text = f"mode={mode}\nsegment={chosen}\n"
        if destination := os.getenv("GITHUB_OUTPUT"):
            with Path(destination).open("a", encoding="utf-8") as handle:
                handle.write(text)
        print(text, end="")
    elif args.action == "collect":
        if args.segment is None:
            parser.error("--segment is required")
        collect(args.segment, args.output)
    else:
        sample = args.action == "sample"
        place = "광화문·덕수궁" if sample else PARK
        reading = safe_query("sample" if sample else required_key(), place)
        if reading is None:
            raise SystemExit("공개 샘플 조회 실패" if sample else "올림픽공원 인증 조회 실패")
        print(f"{place} 응답 확인 · 자료 기준 {reading['PPLTN_TIME']}")


if __name__ == "__main__":
    main()
