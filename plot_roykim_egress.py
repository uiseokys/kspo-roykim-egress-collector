"""Plot the estimated Olympic Park population during the three Roy Kim shows."""

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.ticker import StrMethodFormatter


KST = ZoneInfo("Asia/Seoul")
STEP = timedelta(minutes=5)
REHEARSAL_START = datetime.fromisoformat("2026-11-19 12:00").replace(tzinfo=KST)
SHOW_STARTS = tuple(
    datetime.fromisoformat(value).replace(tzinfo=KST)
    for value in ("2026-11-20 20:00", "2026-11-21 18:00", "2026-11-22 17:00")
)
SEOUL_FIELDS = ("PPLTN_TIME", "AREA_PPLTN_MIN", "AREA_PPLTN_MAX")
MANUAL_FIELDS = ("인구_기준시각_KST", "최소인구", "최대인구")
KOREAN_FONT = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")


def local_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.strip())
    return stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp.astimezone(KST)


def json_rows(value):
    if isinstance(value, list):
        for item in value:
            yield from json_rows(item)
    elif isinstance(value, dict):
        if all(field in value for field in SEOUL_FIELDS):
            yield value
        else:
            for item in value.values():
                yield from json_rows(item)


def read_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        if path.suffix.lower() == ".csv":
            yield from csv.DictReader(source)
        elif path.suffix.lower() == ".json":
            yield from json_rows(json.load(source))
        elif path.suffix.lower() in {".jsonl", ".ndjson"}:
            for line in source:
                if line.strip():
                    yield from json_rows(json.loads(line))
        else:
            raise ValueError(f"지원하지 않는 파일 형식 {path.name}")


def read_observations(paths, fields):
    by_time = {}
    for path in paths:
        for index, row in enumerate(read_rows(path), start=1):
            place = str(row.get("AREA_NM") or "").strip()
            if place and place != "올림픽공원":
                continue
            row_fields = (MANUAL_FIELDS
                          if fields == SEOUL_FIELDS and all(field in row for field in MANUAL_FIELDS)
                          else fields)
            try:
                time_text, min_text, max_text = (str(row[field] or "").strip() for field in row_fields)
                if not (time_text or min_text or max_text):
                    continue
                timestamp = local_time(time_text)
                lower = int(min_text.replace(",", ""))
                upper = int(max_text.replace(",", ""))
                if lower < 0 or upper < lower:
                    raise ValueError("인구 최소·최대 범위 오류")
            except (KeyError, ValueError) as error:
                raise ValueError(f"{path.name} 관측 {index}행: {error}") from error
            bounds = (lower, upper)
            if by_time.setdefault(timestamp, bounds) != bounds:
                raise ValueError(f"{timestamp.isoformat()} 기준시각에 서로 다른 인구 범위가 있습니다.")
    return sorted(by_time.items())


def consecutive_runs(series):
    if not series:
        return
    begin = 0
    for i in range(1, len(series)):
        if series[i][0] - series[i - 1][0] > STEP:
            yield series[begin:i]
            begin = i
    yield series[begin:]


def save_table(path, series_by_show, show_ends, *, metadata=None):
    metadata = metadata or {}
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["공연일", "기준시각_KST", "최소인구", "최대인구",
                         "직전관측_간격_분", "5분_순감소_하한", "5분_순감소_상한", "실제종료_경과분",
                         "REPLACE_YN", "혼잡도"])
        for show, series in series_by_show.items():
            show_metadata = metadata.get(show, {})
            previous = None
            for stamp, (lower, upper) in series:
                gap = (stamp - previous[0]).total_seconds() / 60 if previous else ""
                low_change, high_change = "", ""
                if gap == 5:
                    low_change = previous[1][0] - upper
                    high_change = previous[1][1] - lower
                end = show_ends.get(show.date())
                elapsed = (stamp - end).total_seconds() / 60 if end else ""
                fields = show_metadata.get(stamp, {})
                writer.writerow([show.date(), stamp.isoformat(), lower, upper, gap,
                                 low_change, high_change, elapsed,
                                 fields.get("REPLACE_YN", ""), fields.get("AREA_CONGEST_LVL", "")])
                previous = stamp, (lower, upper)


def save_manual_template(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["회차", "공연시작_KST", "권장_확인시각_KST", "실제_확인시각_KST",
                         *MANUAL_FIELDS, "혼잡도", "스크린샷_파일명", "메모"])
        slots = (timedelta(hours=6, minutes=30) // STEP) + 1
        for show in SHOW_STARTS:
            first = show - timedelta(minutes=90)
            for slot in range(slots):
                stamp = first + slot * STEP
                writer.writerow([show.date(), f"{show:%Y-%m-%d %H:%M}", f"{stamp:%Y-%m-%d %H:%M}",
                                 "", "", "", "", "", "", ""])


def save_figure(path, series_by_show, show_ends):
    if KOREAN_FONT.is_file():
        plt.rcParams["font.family"] = FontProperties(fname=KOREAN_FONT).get_name()
    else:
        plt.rcParams["font.family"] = ["Noto Sans CJK KR", "NanumGothic", "Malgun Gothic", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(len(series_by_show), 1, squeeze=False,
                             figsize=(10, 1.4 + 2.9 * len(series_by_show)), layout="constrained")
    fig.suptitle("로이킴 공연일 올림픽공원 인구 변화", fontsize=19, weight="bold", x=.015, ha="left")
    for ax, (show, series) in zip(axes[:, 0], series_by_show.items()):
        start_label = "리허설 시작" if show == REHEARSAL_START else "공연 시작"
        ax.set_title(f"{show:%m/%d}  ·  {start_label} {show:%H:%M}  ·  기준시각 {len(series)}개",
                     loc="left", fontsize=12, weight="bold")
        if not series:
            ax.text(.5, .5, "수집 파일에 이 회차 관측이 없습니다", ha="center", va="center",
                    transform=ax.transAxes, color="#687785")
            ax.set_axis_off()
            continue
        for i, run in enumerate(consecutive_runs(series)):
            times, bounds = zip(*run)
            lower, upper = zip(*bounds)
            middle = [(a + b) / 2 for a, b in bounds]
            ax.fill_between(times, lower, upper, alpha=.2, color="#3d72cb",
                            label="추정 인구 범위" if i == 0 else None)
            ax.plot(times, middle, ".-", color="#18539f", linewidth=1.5, markersize=3,
                    label="범위 중간값" if i == 0 else None)
            if len(run) == 1:
                ax.vlines(times, lower, upper, color="#3d72cb", linewidth=2)
        ax.axvline(show, color="#718096", linestyle="--", label=start_label)
        end = show_ends.get(show.date())
        if end:
            ax.axvline(end, color="#ab5632", linestyle="--", label=f"확인된 종료 {end:%H:%M}")
        right = max(series[-1][0], end or show, show + STEP * 6) + STEP
        left = min(show - timedelta(minutes=90), series[0][0] - STEP)
        ax.set_xlim(left, right)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=11, tz=KST))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=KST))
        ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
        ax.set_xlabel("자료 기준 시각(KST)")
        ax.set_ylabel("올림픽공원 추정 인구(명)")
        ax.legend(loc="best", ncol=2, fontsize=8, frameon=False)
        ax.grid(axis="y", alpha=.2)
        ax.spines[["top", "right"]].set_visible(False)
    fig.supxlabel("서울시 실시간 인구 · 공원 전체 추정치 · 5분을 넘는 관측 간격은 선을 끊어 표시", fontsize=9)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="*", help="수동 수집표 CSV 또는 서울시 인구 CSV, JSON, JSONL")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--template", type=Path, help="빈 수동 수집표 CSV 생성")
    parser.add_argument("--csv-fields", nargs=3, default=SEOUL_FIELDS,
                        metavar=("TIME", "MIN", "MAX"), help="팀 CSV 열 이름이 다를 때 지정")
    parser.add_argument("--actual-end", action="append", default=[], metavar="YYYY-MM-DDTHH:MM",
                        help="실제 종료 시각이 확인된 회차만 KST로 입력")
    args = parser.parse_args()
    if args.template:
        if args.template.exists():
            parser.error(f"기존 수집표가 있습니다. 다른 경로를 지정하세요. {args.template}")
        save_manual_template(args.template)
        return
    if not args.inputs:
        parser.error("입력 파일을 지정하거나 --template으로 빈 수집표를 생성하세요.")
    ends = {}
    for value in args.actual_end:
        stamp = local_time(value)
        for show in SHOW_STARTS:
            if show <= stamp < show + timedelta(hours=8):
                ends[show.date()] = stamp
                break
        else:
            parser.error(f"로이킴 세 회차에 해당하지 않는 종료 시각 {value}")
    observations = read_observations(args.inputs, args.csv_fields)
    series = {}
    for show in SHOW_STARTS:
        first = show - timedelta(minutes=90)
        last = ends.get(show.date(), show + timedelta(hours=4)) + timedelta(hours=2)
        series[show] = [(stamp, bounds) for stamp, bounds in observations if first <= stamp <= last]
    if not any(series.values()):
        parser.error("세 공연의 수집 구간에 해당하는 공원 인구 관측이 없습니다.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    save_table(args.output_dir / "로이킴_회차별_실시간인구.csv", series, ends)
    save_figure(args.output_dir / "로이킴_회차별_공원인구_퇴장곡선.png", series, ends)
    for show, points in series.items():
        print(f"{show:%Y-%m-%d}: {len(points)}개 기준시각")


if __name__ == "__main__":
    main()
