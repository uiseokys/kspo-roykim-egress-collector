"""조용필 공연일(2026-11-27~29)에 올림픽공원의 서울시 실시간 도시데이터 '전체 항목'을 모읍니다.

로이킴 때의 collector.py는 인구만 받았습니다. 이 수집기는 같은 서비스의 전체 항목(citydata)을 받아
인구·연령대 비율에 더해 지하철·버스 실시간 승하차(최근 5·10·30분), 주차장 현재 대수, 따릉이, 도로 속도, 날씨, 상권 결제를 함께 남깁니다.
공연 시작 1시간 전부터 시작 6시간 뒤까지 2분 간격으로 조회합니다. 표준 라이브러리만 씁니다.

  python3 citydata/collect_citydata.py plan                       # 지금 시각에 맞는 구간 고르기(Actions용)
  python3 citydata/collect_citydata.py collect --segment 1127_조용필-A --push
  python3 citydata/collect_citydata.py check                      # 올림픽공원을 한 번 조회해 항목이 채워지는지 확인
  python3 citydata/collect_citydata.py test --minutes 5 --push    # 짧게 수집해 저장까지 확인

인증키는 환경변수 SEOUL_POPULATION_API_KEY에서만 읽고, 파일이나 로그에 남기지 않습니다.

저장 (citydata/data/<공연>/)
- <구간>-<실행>.csv    조회마다 한 줄 요약(FIELDS)
- <구간>-<실행>.jsonl  추린 원본(KEEP 항목만: 주차장별·따릉이 대여소별 값 포함)
- 전체 응답 원본은 한 번에 15만 글자쯤이라 저장소에 넣지 않고 --full-dir 폴더에 gzip으로 둡니다(Actions 아티팩트로 보관).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent / "data"
API_URL = "http://openapi.seoul.go.kr:8088/{key}/json/citydata/1/5/{area}"
AREA = "올림픽공원"
KEY_NAME = "SEOUL_POPULATION_API_KEY"
INTERVAL = 120  # 호출 간격(초). 자료는 약 5분마다 바뀝니다
SAVE_EVERY = timedelta(minutes=20)  # 이 간격마다 저장소에 올려, 작업이 중간에 끊겨도 자료가 남게 합니다
PLAN_EARLY = timedelta(minutes=45)  # 구간 시작보다 이만큼 일찍 실행돼도 그 구간을 고르고 바로 수집을 시작합니다

# 공연 시작 시각(KST). 구간 A는 시작 1시간 전~시작 3시간 30분 뒤, 구간 B는 시작 2시간 30분 뒤~6시간 뒤(1시간 겹침).
SHOWS = {"1127_조용필": "2026-11-27 19:00", "1128_조용필": "2026-11-28 18:00", "1129_조용필": "2026-11-29 17:00"}
PARTS = {"A": (-60, 210), "B": (150, 360)}


def now_kst() -> datetime:
    return datetime.now(KST)


def log(message: str) -> None:
    print(f"[{now_kst():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def segments() -> dict[str, tuple[datetime, datetime, str]]:
    """구간 이름 → (시작, 끝, 공연)."""
    out = {}
    for label, start in SHOWS.items():
        begin = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        for part, (first, last) in PARTS.items():
            out[f"{label}-{part}"] = (begin + timedelta(minutes=first), begin + timedelta(minutes=last), label)
    return out


def plan(request: str, now: datetime) -> tuple[str, str]:
    """(할 일, 구간). 할 일: collect·check·test·idle. 예약 실행이 구간 밖에서 돌면 idle(실패가 아님)."""
    if request in ("check", "test"):
        return request, ""
    eligible = [name for name, (first, last, _) in segments().items() if first - PLAN_EARLY <= now < last]
    if not eligible:
        return "idle", ""
    return "collect", max(eligible, key=lambda name: segments()[name][0])


# ---------------------------------------------------------------- 응답 다루기

RIDE_WINDOWS = (("5", "5WTHN"), ("10", "10WTHN"), ("30", "30WTHN"), ("acml", "ACML"))  # 최근 5·10·30분, 그날 누적
RIDE_FIELDS = [f"{mode}_{way}_{name}_{edge}" for mode in ("sub", "bus") for name, _ in RIDE_WINDOWS for way in ("on", "off") for edge in ("min", "max")]
FIELDS = [
    "fetched_at_kst", "ppltn_time", "area_cd", "area_nm", "congest_lvl", "ppltn_min", "ppltn_max", "male_rate", "female_rate",
    "rate_0", "rate_10", "rate_20", "rate_30", "rate_40", "rate_50", "rate_60", "rate_70", "resnt_rate", "non_resnt_rate", "replace_yn",
    "sub_time", "sub_stn_cnt", "bus_time", "bus_stn_cnt", *RIDE_FIELDS,
    "prk_lots", "prk_capacity", "prk_current", "prk_time",  # 실시간 연동된 주차장만 더한 값
    "bike_spots", "bike_parked", "road_spd", "road_idx", "road_time", "temp", "precpt_type", "weather_time",
    "cmrcl_lvl", "cmrcl_payment_cnt", "cmrcl_time", "result_code", "status",
]
# 추린 원본에 남길 항목: 항목 이름 → 남길 열(None이면 그대로)
KEEP = {
    "LIVE_PPLTN_STTS": None, "LIVE_SUB_PPLTN": None, "LIVE_BUS_PPLTN": None,
    "PRK_STTS": ["PRK_NM", "PRK_CD", "PRK_TYPE", "CPCTY", "CUR_PRK_CNT", "CUR_PRK_TIME", "CUR_PRK_YN", "LNG", "LAT"],
    "SBIKE_STTS": ["SBIKE_SPOT_NM", "SBIKE_SPOT_ID", "SBIKE_PARKING_CNT", "SBIKE_RACK_CNT", "SBIKE_SHARED"],
    "ROAD_TRAFFIC_STTS": ["AVG_ROAD_DATA"],
    "WEATHER_STTS": ["WEATHER_TIME", "TEMP", "HUMIDITY", "WIND_SPD", "PRECIPITATION", "PRECPT_TYPE", "PCP_MSG"],
    "LIVE_CMRCL_STTS": None,
}
DROP_INSIDE = {"FCST_PPLTN"}  # 인구 예측 12개(용량만 큼)


def fetch(key: str, area: str = AREA, timeout: int = 30, retries: int = 3) -> dict:
    """전체 항목을 호출해 JSON을 돌려줍니다. 실패하면 키가 들어가지 않은 짧은 오류만 냅니다."""
    url = API_URL.format(key=urllib.parse.quote(key, safe=""), area=urllib.parse.quote(area, safe=""))
    last_error = "unknown"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            last_error = f"연결 실패 ({type(exc.reason).__name__})"
        except (TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = type(exc).__name__
        if attempt < retries:
            time.sleep(5 * attempt)
    raise RuntimeError(last_error)  # 주소(키 포함)는 넣지 않습니다


def _one(value) -> dict:
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _city(data: dict) -> dict:
    city = data.get("CITYDATA") if isinstance(data, dict) else None
    return city if isinstance(city, dict) else {}


def to_row(data: dict, fetched_at: datetime) -> dict:
    """응답 하나를 요약 한 줄로. 인구가 없으면 status는 empty(다른 항목은 있는 대로 채움)."""
    result = (data.get("RESULT") or {}) if isinstance(data, dict) else {}
    city = _city(data)
    row = {field: "" for field in FIELDS}
    row["fetched_at_kst"] = fetched_at.strftime("%Y-%m-%d %H:%M:%S")
    row["result_code"] = result.get("RESULT.CODE") or result.get("CODE") or ""
    people = _one(city.get("LIVE_PPLTN_STTS"))
    row.update({
        "ppltn_time": people.get("PPLTN_TIME", ""), "area_cd": people.get("AREA_CD", ""), "area_nm": people.get("AREA_NM", ""),
        "congest_lvl": people.get("AREA_CONGEST_LVL", ""), "ppltn_min": people.get("AREA_PPLTN_MIN", ""), "ppltn_max": people.get("AREA_PPLTN_MAX", ""),
        "male_rate": people.get("MALE_PPLTN_RATE", ""), "female_rate": people.get("FEMALE_PPLTN_RATE", ""),
        "resnt_rate": people.get("RESNT_PPLTN_RATE", ""), "non_resnt_rate": people.get("NON_RESNT_PPLTN_RATE", ""), "replace_yn": people.get("REPLACE_YN", ""),
    })
    for age in (0, 10, 20, 30, 40, 50, 60, 70):
        row[f"rate_{age}"] = people.get(f"PPLTN_RATE_{age}", "")
    for mode, section in (("sub", "LIVE_SUB_PPLTN"), ("bus", "LIVE_BUS_PPLTN")):
        ride, prefix = _one(city.get(section)), mode.upper()
        row[f"{mode}_time"], row[f"{mode}_stn_cnt"] = ride.get(f"{prefix}_STN_TIME", ""), ride.get(f"{prefix}_STN_CNT", "")
        for name, code in RIDE_WINDOWS:
            for way, word in (("on", "GTON"), ("off", "GTOFF")):
                for edge in ("min", "max"):
                    row[f"{mode}_{way}_{name}_{edge}"] = ride.get(f"{prefix}_{code}_{word}_PPLTN_{edge.upper()}", "")
    lots = [p for p in city.get("PRK_STTS") or [] if isinstance(p, dict) and p.get("CUR_PRK_YN") == "Y" and _number(p.get("CUR_PRK_CNT")) is not None]
    if lots:
        row["prk_lots"] = len(lots)
        row["prk_capacity"] = round(sum(_number(p.get("CPCTY")) or 0 for p in lots))
        row["prk_current"] = round(sum(_number(p["CUR_PRK_CNT"]) for p in lots))
        row["prk_time"] = max(str(p.get("CUR_PRK_TIME", "")) for p in lots)
    bikes = [b for b in city.get("SBIKE_STTS") or [] if isinstance(b, dict) and _number(b.get("SBIKE_PARKING_CNT")) is not None]
    if bikes:
        row["bike_spots"], row["bike_parked"] = len(bikes), round(sum(_number(b["SBIKE_PARKING_CNT"]) for b in bikes))
    traffic = city.get("ROAD_TRAFFIC_STTS")
    road = _one(traffic.get("AVG_ROAD_DATA") if isinstance(traffic, dict) else None)
    row["road_spd"], row["road_idx"], row["road_time"] = road.get("ROAD_TRAFFIC_SPD", ""), road.get("ROAD_TRAFFIC_IDX", ""), road.get("ROAD_TRAFFIC_TIME", "")
    weather = _one(city.get("WEATHER_STTS"))
    row["temp"], row["precpt_type"], row["weather_time"] = weather.get("TEMP", ""), weather.get("PRECPT_TYPE", ""), weather.get("WEATHER_TIME", "")
    shops = _one(city.get("LIVE_CMRCL_STTS"))
    row["cmrcl_lvl"], row["cmrcl_payment_cnt"], row["cmrcl_time"] = shops.get("AREA_CMRCL_LVL", ""), shops.get("AREA_SH_PAYMENT_CNT", ""), shops.get("CMRCL_TIME", "")
    has_people = bool(row["ppltn_time"]) and row["ppltn_min"] not in ("", None) and row["ppltn_max"] not in ("", None)
    row["status"] = "ok" if has_people else "empty"
    return row


def trim(data: dict) -> dict:
    """저장소에 넣을 만큼만 추린 원본: KEEP에 적은 항목과 열만 남깁니다."""
    city, out = _city(data), {}
    for section, columns in KEEP.items():
        value = city.get(section)
        if value in (None, [], {}):
            continue

        def pick(item, columns=columns):
            if not isinstance(item, dict):
                return item
            return {k: v for k, v in item.items() if (columns is None or k in columns) and k not in DROP_INSIDE}
        out[section] = [pick(item) for item in value] if isinstance(value, list) else pick(value)
    return {"AREA_NM": city.get("AREA_NM", ""), "AREA_CD": city.get("AREA_CD", ""), **out}


def describe(row: dict) -> str:
    def span(name):  # 값이 비어 있으면(올림픽공원은 지하철·주차장 실시간 값이 비어 있을 때가 있음) '없음'
        low, high = row[f"{name}_min"], row[f"{name}_max"]
        return "없음" if low in ("", None) else f"{low}~{high}"
    return (f"{row['ppltn_time']} 인구 {span('ppltn')} · 지하철 5분 승차 {span('sub_on_5')} · 버스 5분 승차 {span('bus_on_5')}"
            f" · 주차 {row['prk_current'] if row['prk_lots'] else '없음'} · 따릉이 {row['bike_parked'] if row['bike_spots'] else '없음'}대")


# ---------------------------------------------------------------- 저장

def append_csv(path: Path, row: dict) -> None:
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def append_jsonl(path: Path, fetched_at: datetime, payload: dict, compress: bool = False) -> None:
    record = json.dumps({"fetched_at_kst": fetched_at.strftime("%Y-%m-%d %H:%M:%S"), "response": payload}, ensure_ascii=False) + "\n"
    with (gzip.open if compress else open)(path, "at", encoding="utf-8") as f:
        f.write(record)


def save_to_repo(paths: list[Path], note: str) -> bool:
    """수집 파일을 저장소 main에 올립니다(Actions에서만). 다른 실행이 먼저 올렸으면 받아서 다시 올립니다."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    existing = [str(p.relative_to(ROOT)) for p in paths if p.is_file()]
    if not existing:
        return True
    git("add", "--", *existing)
    if git("diff", "--cached", "--quiet").returncode == 0:
        return True
    if git("commit", "--quiet", "-m", note).returncode != 0:
        return False
    for attempt in range(4):
        if git("pull", "--quiet", "--rebase", "origin", "main").returncode == 0 and git("push", "--quiet", "origin", "HEAD:main").returncode == 0:
            return True
        git("rebase", "--abort")
        time.sleep(5 * (attempt + 1))
    return False


def run(key: str, label: str, name: str, until: datetime, interval: int = INTERVAL, push: bool = False, full_dir: Path | None = None) -> int:
    """until까지 interval초마다 조회해 OUT_DIR/<label>/<name>.csv·.jsonl에 쌓습니다. 성공한 조회가 있고 저장도 됐으면 0."""
    folder = OUT_DIR / label
    folder.mkdir(parents=True, exist_ok=True)
    csv_path, jsonl_path = folder / f"{name}.csv", folder / f"{name}.jsonl"
    full_path = None
    if full_dir is not None:
        full_dir.mkdir(parents=True, exist_ok=True)
        full_path = full_dir / f"{label}-{name}.jsonl.gz"
    log(f"수집 시작: {label} {name} (끝 {until:%m/%d %H:%M}, 간격 {interval}초)")
    ok = fail = 0
    saved, last_save = True, now_kst()
    while now_kst() < until:
        fetched_at = now_kst()
        try:
            data = fetch(key)
            row = to_row(data, fetched_at)
            if full_path is not None:
                append_jsonl(full_path, fetched_at, data, compress=True)
            if row["status"] == "ok":
                append_jsonl(jsonl_path, fetched_at, trim(data))
                ok += 1
                log(describe(row))
            else:
                fail += 1
                log(f"유효한 인구 관측이 없는 응답 ({row['result_code'] or '코드 없음'})")
        except Exception as exc:  # 한 번 실패해도 멈추지 않습니다. 오류 문구에는 키가 없습니다(fetch 참고)
            row = {field: "" for field in FIELDS}
            row["fetched_at_kst"] = fetched_at.strftime("%Y-%m-%d %H:%M:%S")
            row["status"] = f"error: {type(exc).__name__}"
            fail += 1
            log(row["status"])
        append_csv(csv_path, row)
        if push and now_kst() - last_save >= SAVE_EVERY:
            saved = save_to_repo([csv_path, jsonl_path], f"수집: {label} {name} (진행 중)")
            last_save = now_kst()
        remaining = (until - now_kst()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    if push:
        saved = save_to_repo([csv_path, jsonl_path], f"수집: {label} {name}")
        log("저장소에 올림" if saved else "저장소에 올리지 못함(아티팩트에는 남음)")
    log(f"수집 종료: 성공 {ok}회, 실패 {fail}회 → {csv_path.relative_to(ROOT)}")
    return 0 if ok > 0 and saved else 1


# ---------------------------------------------------------------- 실행

def _output(**values: str) -> None:
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with open(destination, "a", encoding="utf-8") as handle:
            for name, value in values.items():
                print(f"{name}={value}", file=handle)


def _key() -> str:
    key = os.environ.get(KEY_NAME, "").strip()
    if not key:
        raise SystemExit(f"저장소 Actions Secrets에 {KEY_NAME}를 등록하세요.")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description="조용필 공연일 올림픽공원 도시데이터 전체 항목 수집")
    parser.add_argument("action", choices=("plan", "collect", "check", "test"))
    parser.add_argument("--segment", choices=sorted(segments()))
    parser.add_argument("--minutes", type=int, default=5, help="test: 수집할 시간(분)")
    parser.add_argument("--push", action="store_true", help="수집 파일을 저장소 main에 올림(Actions용)")
    parser.add_argument("--full-dir", type=Path, help="전체 응답 원본(gzip)을 둘 폴더")
    args = parser.parse_args()
    run_name = f"{os.environ.get('GITHUB_RUN_ID') or now_kst().strftime('%Y%m%dT%H%M%S')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"

    if args.action == "plan":
        mode, chosen = plan(os.environ.get("RUN_REQUEST", ""), now_kst())
        log(f"이벤트 {os.environ.get('RUN_EVENT', 'local')} · 요청 {os.environ.get('RUN_REQUEST') or '—'} · 할 일 {mode} {chosen}")
        _output(mode=mode, segment=chosen)
        return 0
    if args.action == "check":
        row = to_row(fetch(_key()), now_kst())
        log(f"올림픽공원 조회 {row['status']} ({row['result_code']}) · {describe(row)}")
        log(f"채워진 열 {sum(1 for v in row.values() if v != '')}/{len(FIELDS)}")
        return 0 if row["status"] == "ok" else 1
    if args.action == "test":
        return run(_key(), f"{now_kst():%m%d}_test", f"test-{run_name}", now_kst() + timedelta(minutes=args.minutes), 60, args.push, args.full_dir)
    if args.segment is None:
        parser.error("collect에는 --segment가 필요합니다.")
    first, last, label = segments()[args.segment]
    now = now_kst()
    if now >= last:
        log(f"{args.segment} 구간은 이미 끝났습니다({last:%m/%d %H:%M}). 할 일 없음.")
        return 0
    if now < first - PLAN_EARLY:
        log(f"{args.segment} 구간은 아직 멀었습니다({first:%m/%d %H:%M} 시작). 할 일 없음.")
        return 0
    _output(folder=str((OUT_DIR / label).relative_to(ROOT)))
    return run(_key(), label, f"{args.segment.rsplit('-', 1)[1]}-{run_name}", last, INTERVAL, args.push, args.full_dir)


if __name__ == "__main__":
    sys.exit(main())
