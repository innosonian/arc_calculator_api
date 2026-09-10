"""콘솔 전용 훈련 결과 뷰어 — 메타 목록에서 골라 터미널에 결과 화면을 출력 (의존성 0)

사용:
    .venv/bin/python scripts/console_viewer.py [디렉토리] [--passing 80]

디렉토리(기본 .)에서 *.meta.json + *.bin 쌍을 찾아 목록 패널을 띄운다.
번호를 입력하면 메타 정보(vp_event_list 제외)와 결과 화면(요약 점수 ·
Time Series · Metric Criteria · Score By Cycle)을 ANSI 컬러로 출력한다.

Time Series는 피벗 표 — 열 = 사이클(시간 순), 행 = #n번째 압박(깊이 mm)과
vn번째 환기(량 ml). 값 색으로 정상(초록)/VP(보라)/범위 밖(빨강)을 구분한다.

목록 명령: [번호] 선택 · [n]/[p] 페이지 · [f 검색어] 필터 · [f] 해제 · [q] 종료
색상: 초록=정상, 보라=가상파트너(VP), 빨강=범위 밖 — 앱 차트와 동일한 규칙.
"""
# 원본: hstm_v2 scripts/console_viewer.py (arc 이식 — 동작 동일).

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

from viewer_common import DEPTH_BAND_MM, VOLUME_BAND_ML, band, criteria_rows, fmt_score, metric_rows, run_training, scan

PAGE_SIZE = 20
BLOCKS = "▁▂▃▄▅▆▇█"


def c(text, *codes) -> str:
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def _terminal_width(default: int = 110) -> int:
    """현재 창 너비. 실제 크기(ioctl)를 COLUMNS 환경변수보다 우선한다 —
    csh/tcsh 등이 COLUMNS를 export하면 창을 키워도 옛 값이 잡히는 문제 방지."""
    import os
    try:
        return os.get_terminal_size(sys.stdout.fileno()).columns
    except (OSError, ValueError):
        return shutil.get_terminal_size((default, 30)).columns


GREEN, PURPLE, RED, BLUE, DIM, BOLD, CYAN = "92", "95", "91", "94", "2", "1", "36"


# ───────────────────────── 목록 패널 ─────────────────────────


def render_list(entries, page, keyword):
    total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    print()
    header = f"훈련 목록 {len(entries)}건 — {page + 1}/{total_pages} 페이지"
    if keyword:
        header += f"  (필터: {keyword})"
    print(c(header, BOLD))
    print(c("─" * 78, DIM))
    for i, entry in enumerate(entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], start=page * PAGE_SIZE + 1):
        print(f"  {c(f'{i:3d}', BLUE)}  {entry['kst']}  {entry['label']}  {c(entry['base'][:30] + '…', DIM)}")
    print(c("─" * 78, DIM))
    print(c("명령: [번호] 선택 · [n] 다음 페이지 · [p] 이전 페이지 · [f 검색어] 필터 · [f] 필터 해제 · [q] 종료", CYAN))
    return page, total_pages


# ───────────────────────── 결과 화면 ─────────────────────────


def show_meta(entry):
    meta = {k: v for k, v in entry["meta"].items() if k != "vp_event_list"}
    vp_n = len(entry["meta"].get("vp_event_list") or [])
    print(c("\n■ 메타 정보", BOLD))
    print(f"  파일   : {entry['base']}")
    print(f"  시각   : {entry['kst']} KST")
    cond = meta.pop("condition", {})
    print("  조건   : " + "  ".join(f"{k}={v}" for k, v in cond.items()))
    for k, v in meta.items():
        print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
    if vp_n:
        print(c(f"  (vp_event_list {vp_n}건 생략)", DIM))


def show_summary(result, condition, passing):
    ts = result["cpr_score"]["total_score"]
    overall = ts.get("overall") or 0
    judg = ts.get("judg_result")
    print(c("\n■ Score", BOLD))
    color = GREEN if overall >= passing else RED
    print(f"  {c(str(overall), color, BOLD)}  (passing {passing}, 판정 {judg})")
    for label, key in criteria_rows(condition):
        score = ts.get(key)
        n = 0 if score is None else int(score)
        bar = "█" * (n * 30 // 100)
        print(f"  {label:22s} {c(bar.ljust(30, ' '), GREEN if n >= passing else RED)} {fmt_score(score):>4s}")
    feedback = list(result.get("guide_prompts") or [])
    if overall >= passing:
        feedback.insert(0, "Well done! You passed!")
    if feedback:
        print(c("\n■ Feedback", BOLD))
        for line in feedback:
            print(f"  {line}")


def _bar_color(is_vp, value, lo, hi):
    if is_vp:
        return PURPLE
    return GREEN if lo <= value <= hi else RED


GRAPH_ROWS = 3  # 그래프 높이(줄) — 줄당 8단계 = 총 24단계 해상도


def show_time_series(chart, condition):
    """피벗 표 — 열 = 사이클(시간 순). 머리글 아래 사이클별 그래프(3줄 블록) 행,
    이어서 #n번째 압박(깊이 mm)·vn번째 환기(량 ml) 수치 행.
    그래프 폭은 터미널 너비에 맞춰 자동 조정(넓으면 압박 1회=여러 칸, 좁으면 다운샘플)."""
    depth_min, depth_max, depth_axis = band(condition, DEPTH_BAND_MM)
    vol_min, vol_max, vol_axis = band(condition, VOLUME_BAND_ML)
    data = chart.get("cpr_data_set") or []
    if not data:
        return

    groups = {}
    order = []
    for d in data:
        key = (d["part_num"], d["cycle_num"])
        if key not in groups:
            groups[key] = {"comp": [], "vent": []}
            order.append(key)
        groups[key][d["action_type"]].append(d)

    base = []  # (머리글, vp 여부, comps, vents)
    for part, cyc in order:
        g = groups[(part, cyc)]
        comps, vents = g["comp"], g["vent"]
        is_vp = bool(comps) and sum(1 for d in comps if d["is_virtual_action"]) * 2 > len(comps)
        base.append((f"c{cyc}" + ("·VP" if is_vp else ""), is_vp, comps, vents))

    # 창 너비에 맞춰 그래프 배율 결정 — per/inv = 압박 1회당 칸 수 / 1칸당 압박 수
    label_w, sep_plain = 7, 3
    term_w = _terminal_width()

    def graph_width(n, per, inv):
        return math.ceil(n * per) if per else math.ceil(n / inv)

    for per, inv in ((3, 0), (2, 0), (1, 0), (0, 2), (0, 3), (0, 4), (0, 6), (0, 8)):
        widths = [max(len(lb), graph_width(len(cp), per, inv), 4) for lb, _vp, cp, _v in base]
        if label_w + sum(widths) + sep_plain * (len(widths) - 1) <= term_w:
            break

    sep = c(" │ ", DIM)
    label_pad = " " * label_w

    def pad(colored_text, plain_len, width):
        return colored_text + " " * (width - plain_len)

    print(c("\n■ Time Series Chart", BOLD))
    print(c(f"  열=사이클(시간 순) · 그래프=압박 깊이 파형"
            + (f"(1칸≈압박 {inv}회)" if inv > 1 else (f"(압박 1회={per}칸)" if per > 1 else ""))
            + f" · 행=#n 압박(깊이 mm), vn 환기(량 ml) · "
            f"양호 {depth_min}~{depth_max}mm, {vol_min}~{vol_max}ml · {c('값', GREEN)}=정상 {c('값', PURPLE)}=VP {c('값', RED)}=범위 밖", DIM))

    # 사이클별 그래프 칸(buckets): 칸 값 = 구간 최대 깊이, 색 = VP > 범위 밖 > 정상
    graph_cols = []
    for (lb, _vp, comps, _v), w in zip(base, widths):
        n = len(comps)
        g_w = graph_width(n, per, inv) if n else 0
        buckets = []
        for j in range(g_w):
            lo, hi = j * n // g_w, max(j * n // g_w + 1, (j + 1) * n // g_w)
            seg = comps[lo:hi]
            mm = max(d["comp_depth_max"] / 2 for d in seg)
            if seg[0]["is_virtual_action"]:
                color = PURPLE
            elif any(not depth_min <= d["comp_depth_max"] / 2 <= depth_max for d in seg):
                color = RED
            else:
                color = GREEN
            buckets.append((mm, color))
        graph_cols.append(buckets)

    print(label_pad + sep.join(pad(c(lb, (PURPLE if vp else BLUE), BOLD), len(lb), w)
                               for (lb, vp, _c2, _v), w in zip(base, widths)))
    # 3줄 그래프: 위 줄부터, 칸 높이 = 깊이/축최대 × 24단계
    levels = GRAPH_ROWS * len(BLOCKS)
    for r in range(GRAPH_ROWS - 1, -1, -1):
        cells = []
        for buckets, w in zip(graph_cols, widths):
            chars = []
            for mm, color in buckets:
                filled = min(levels, max(0, round(mm / depth_axis * levels))) - r * len(BLOCKS)
                chars.append(c(BLOCKS[min(filled, len(BLOCKS)) - 1], color) if filled > 0 else " ")
            cells.append(pad("".join(chars), len(buckets), w))
        print(label_pad + sep.join(cells))

    n_comp = max((len(cp) for _l, _vp, cp, _v in base), default=0)
    n_vent = max((len(v) for _l, _vp, _c2, v in base), default=0)
    for i in range(n_comp):
        row = f"  {('#' + str(i + 1)):>4} "
        cells = []
        for (_l, _vp, comps, _v), w in zip(base, widths):
            if i < len(comps):
                d = comps[i]
                mm = d["comp_depth_max"] / 2
                text = f"{mm:.0f}"
                cells.append(pad(c(text, _bar_color(d["is_virtual_action"], mm, depth_min, depth_max)), len(text), w))
            else:
                cells.append(pad(c("-", DIM), 1, w))
        print(row + sep.join(cells))
    for j in range(n_vent):
        row = f"  {('v' + str(j + 1)):>4} "
        cells = []
        for (_l, _vp, _c2, vents), w in zip(base, widths):
            if j < len(vents):
                d = vents[j]
                vol = d["vent_vol_max"]
                text = f"{vol}"
                cells.append(pad(c(text, _bar_color(d["is_virtual_action"], vol, vol_min, vol_max)), len(text), w))
            else:
                cells.append(pad(c("-", DIM), 1, w))
        print(row + sep.join(cells))


def show_metric_criteria(result, condition):
    print(c("\n■ CPR Metric Criteria Chart", BOLD))
    seg_w = 24  # 고정폭 — 행이 달라도 구분선(│)이 세로로 정렬되도록
    for title, segments in metric_rows(result["metrics"], condition):
        cells = []
        for label, value in segments:
            is_good = label in ("Good", "Score")
            nonzero = not value.startswith("0%") and value != "0"
            text = f"{label} {value}".ljust(seg_w)
            if is_good:
                cells.append(c(text, BLUE, BOLD))
            elif nonzero:
                cells.append(c(text, BOLD))
            else:
                cells.append(c(text, DIM))
        print(f"  {title:22s} {c('│', DIM)} ".rstrip() + " " + (" " + c("│", DIM) + " ").join(cells))


def show_score_by_cycle(result, condition):
    ts = result["cpr_score"]["total_score"]
    cycles = [cyc for part in result["cpr_score"]["part_scores"] for cyc in part["cycle_with_score_list"]]
    print(c("\n■ CPR Score By Cycle", BOLD))
    width = 7
    head = "  " + " " * 22 + c("Overall".rjust(width), BLUE, BOLD)
    head += "".join(c(f"Cyc{i + 1}".rjust(width), BLUE) for i in range(len(cycles)))
    print(head)
    print("  " + " " * 22 + c(fmt_score(ts.get('overall')).rjust(width), BOLD)
          + "".join(fmt_score(cyc.get("overall")).rjust(width) for cyc in cycles))
    for label, key in criteria_rows(condition):
        row = f"  {label:22s}" + fmt_score(ts.get(key)).rjust(width)
        row += "".join(fmt_score(cyc.get(key)).rjust(width) for cyc in cycles)
        print(row)


def show_result(entry, passing):
    print(c(f"\n{'═' * 78}", DIM))
    show_meta(entry)
    print(c("\n  계산 중…", DIM), end="", flush=True)
    condition = entry["meta"]["condition"]
    result, chart = run_training(str(entry["bin"]), condition, entry["meta"].get("vp_event_list") or [])
    print("\r" + " " * 12 + "\r", end="")
    show_summary(result, condition, passing)
    show_time_series(chart, condition)
    show_metric_criteria(result, condition)
    show_score_by_cycle(result, condition)
    print(c(f"{'═' * 78}", DIM))


# ───────────────────────── 메인 루프 ─────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", nargs="?", default=".", help="*.meta.json + *.bin 디렉토리 (기본 .)")
    parser.add_argument("--passing", type=int, default=80, help="합격 기준선 (기본 80)")
    args = parser.parse_args()

    # 콘솔 뷰어는 조건을 알 수 있는(메타 있는) 훈련만 다룬다 — 메타 없는 .bin은 run_local.py로 수동 조건 계산
    all_entries = [e for e in scan(Path(args.directory)) if e["has_meta"]]
    if not all_entries:
        print(f"{args.directory} 에서 *.meta.json + *.bin 쌍을 찾지 못했습니다.")
        return 1

    entries, keyword, page = all_entries, "", 0
    while True:
        page, total_pages = render_list(entries, page, keyword)
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if cmd == "q":
            return 0
        if cmd == "n":
            page = min(page + 1, total_pages - 1)
            continue
        if cmd == "p":
            page = max(page - 1, 0)
            continue
        if cmd.startswith("f"):
            keyword = cmd[1:].strip()
            entries = [e for e in all_entries if keyword.lower() in (e["base"] + e["label"]).lower()] if keyword else all_entries
            page = 0
            continue
        if cmd.isdigit() and 1 <= int(cmd) <= len(entries):
            show_result(entries[int(cmd) - 1], args.passing)
            try:
                back = input(c("명령: [Enter] 목록으로 · [q] 종료 > ", CYAN)).strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if back == "q":
                return 0
            continue
        print(c("잘못된 입력입니다. 명령: [번호] 선택 · [n]/[p] 페이지 · [f 검색어] 필터 · [q] 종료", RED))


if __name__ == "__main__":
    sys.exit(main())
