#!/usr/bin/env python3
"""
부동산중개업 월별 개업/폐업/영업중단 집계 스크립트 (v2)

사용법:
  1. 매월 1일 전후, 전국 부동산중개업 CSV를 다운로드
  2. snapshots/ 폴더에 "YYYY-MM.csv"로 저장 (예: snapshots/2026-08.csv)
  3. (선택) 과거 수치를 backfill.csv에 입력
  4. python aggregate.py 실행 → data.json 생성

집계 방식:
  - 개업: 각 사무소의 '등록일자'를 월 단위로 집계
  - 폐업: 연속 스냅샷 비교 — 이전 달에 있던 등록번호가 사라진 경우
  - 영업 중단: 연속 스냅샷 비교 — 상태가 새로 휴업/휴업연장/업무정지로 바뀐 경우
  - backfill.csv: 스냅샷 비교가 불가능한 과거 구간을 외부 수치로 채움
    (형식: 월,지역,개업,폐업 — 예: 2026-01,전국,875,1168)
  - 우선순위: 자체 스냅샷 집계 > backfill > 등록일자 기반 개업만
"""

import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

SNAP_DIR = Path("snapshots")
BACKFILL_FILE = Path("backfill.csv")
OUT_FILE = Path("data.json")
MONTHS_WINDOW = 36  # 대시보드에 표시할 최근 개월 수

# 데이터 출처에 따라 컬럼명이 다르므로 후보를 두고 자동 탐색합니다.
REGNO_CANDIDATES = ["개설등록번호", "등록번호", "중개업등록번호"]
OPENDATE_CANDIDATES = ["개설등록일자", "등록일자", "개설등록일"]
DONG_CANDIDATES = ["법정동명"]
DONGCODE_CANDIDATES = ["법정동코드"]
ADDR_CANDIDATES = ["소재지지번주소", "지번주소", "소재지도로명주소", "도로명주소", "소재지", "주소"]
STATUS_CANDIDATES = ["상태구분명", "영업상태명", "영업상태", "영업상태구분", "상태구분"]

# 법정동코드 앞 2자리 → 시도 (주소가 모두 비어있을 때의 최후 폴백)
SIDO_CODE = {
    "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시",
    "29": "광주광역시", "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시",
    "41": "경기도", "42": "강원특별자치도", "51": "강원특별자치도",
    "43": "충청북도", "44": "충청남도", "45": "전북특별자치도", "52": "전북특별자치도",
    "46": "전라남도", "47": "경상북도", "48": "경상남도", "50": "제주특별자치도",
}

SIDO_NORMALIZE = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "강원도": "강원특별자치도",
    "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도", "전라북도": "전북특별자치도",
    "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도", "제주도": "제주특별자치도",
}

# 상태 문자열 → 카테고리
def status_cat(status: str) -> str:
    s = status or ""
    if "정지" in s:
        return "stop"        # 업무정지
    if "연장" in s:
        return "ext"         # 휴업 연장
    if "휴업" in s:
        return "pause"       # 휴업
    if "폐업" in s or "말소" in s or "취소" in s:
        return "closed"
    return "active"          # 영업중 (상태 없음 포함)


def read_csv_rows(path: Path) -> list[dict]:
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            if rows and len(rows[0].keys()) > 1:
                return rows
        except (UnicodeDecodeError, LookupError):
            continue
    raise SystemExit(f"[오류] {path} 인코딩을 판별하지 못했습니다.")


def pick_col(row: dict, candidates: list[str]) -> str | None:
    keys = {k.strip(): k for k in row.keys() if k}
    for cand in candidates:
        if cand in keys:
            return keys[cand]
    for cand in candidates:
        for k in keys:
            if cand in k:
                return keys[k]
    return None


def parse_month(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) >= 6:
        y, m = digits[:4], digits[4:6]
        if "1900" <= y <= "2100" and "01" <= m <= "12":
            return f"{y}-{m}"
    return None


def parse_sido(row: dict, cols: dict) -> str:
    """법정동명 → 지번/도로명주소 → 법정동코드 순으로 시도를 판별."""
    for key in ("dong", "addr"):
        c = cols.get(key)
        if c:
            raw = (row.get(c) or "").strip()
            if raw:
                token = raw.split()[0]
                sido = SIDO_NORMALIZE.get(token, token)
                if sido.endswith(("시", "도")):
                    return sido
    c = cols.get("code")
    if c:
        code = re.sub(r"[^0-9]", "", row.get(c) or "")[:2]
        if code in SIDO_CODE:
            return SIDO_CODE[code]
    return "기타"


def load_snapshot(path: Path) -> dict[str, dict]:
    rows = read_csv_rows(path)
    first = rows[0]
    c_reg = pick_col(first, REGNO_CANDIDATES)
    c_open = pick_col(first, OPENDATE_CANDIDATES)
    cols = {
        "dong": pick_col(first, DONG_CANDIDATES),
        "addr": pick_col(first, ADDR_CANDIDATES),
        "code": pick_col(first, DONGCODE_CANDIDATES),
    }
    c_stat = pick_col(first, STATUS_CANDIDATES)
    if not c_reg:
        raise SystemExit(
            f"[오류] {path.name}에서 등록번호 컬럼을 찾지 못했습니다.\n"
            f"       실제 컬럼: {list(first.keys())}\n"
            f"       → aggregate.py 상단의 REGNO_CANDIDATES에 컬럼명을 추가하세요."
        )
    out = {}
    for r in rows:
        reg = (r.get(c_reg) or "").strip()
        if not reg:
            continue
        status = (r.get(c_stat) or "").strip() if c_stat else ""
        out[reg] = {
            "open_month": parse_month(r.get(c_open, "")) if c_open else None,
            "sido": parse_sido(r, cols),
            "cat": status_cat(status),
        }
    return out


def load_backfill() -> dict:
    """(sido, month) → {"open": n, "close": n}"""
    if not BACKFILL_FILE.exists():
        return {}
    out = {}
    rows = read_csv_rows(BACKFILL_FILE)
    for r in rows:
        keys = {k.strip(): v for k, v in r.items() if k}
        month = (keys.get("월") or keys.get("month") or "").strip()
        sido = (keys.get("지역") or keys.get("region") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}", month) or not sido:
            continue
        sido = SIDO_NORMALIZE.get(sido, sido)

        def num(*names):
            for n in names:
                v = (keys.get(n) or "").replace(",", "").strip()
                if v:
                    try:
                        return int(v)
                    except ValueError:
                        pass
            return None

        out[(sido, month)] = {"open": num("개업", "open"), "close": num("폐업", "close")}
    return out


def month_range(start: str, end: str) -> list[str]:
    ys, ms = int(start[:4]), int(start[5:7])
    ye, me = int(end[:4]), int(end[5:7])
    result = []
    while (ys, ms) <= (ye, me):
        result.append(f"{ys:04d}-{ms:02d}")
        ms += 1
        if ms == 13:
            ys, ms = ys + 1, 1
    return result


def main():
    files = sorted(SNAP_DIR.glob("*.csv"))
    files = [f for f in files if re.fullmatch(r"\d{4}-\d{2}", f.stem)]
    if not files:
        raise SystemExit("[안내] snapshots/ 폴더에 YYYY-MM.csv 파일이 없습니다. 예: snapshots/2026-07.csv")

    print(f"스냅샷 {len(files)}개 발견: {[f.stem for f in files]}")
    snaps = {f.stem: load_snapshot(f) for f in files}
    snap_months = sorted(snaps.keys())
    latest = snaps[snap_months[-1]]
    backfill = load_backfill()
    if backfill:
        bf_months = sorted({m for (_, m) in backfill})
        print(f"backfill.csv 로드: {len(backfill)}행 (기간 {bf_months[0]} ~ {bf_months[-1]})")

    # ── 개업 (등록일자 기반, 모든 스냅샷 합산)
    registry: dict[str, dict] = {}
    for m in snap_months:
        for reg, info in snaps[m].items():
            if reg not in registry or (info["open_month"] and not registry[reg]["open_month"]):
                registry[reg] = info

    reg_open = defaultdict(lambda: defaultdict(int))
    for info in registry.values():
        om = info["open_month"]
        if om:
            reg_open[info["sido"]][om] += 1
            reg_open["전국"][om] += 1

    # ── 폐업 및 영업 중단 전환 (연속 스냅샷 diff)
    own_close = defaultdict(lambda: defaultdict(int))
    s_pause = defaultdict(lambda: defaultdict(int))   # 신규 휴업
    s_ext = defaultdict(lambda: defaultdict(int))     # 휴업 연장
    s_stop = defaultdict(lambda: defaultdict(int))    # 업무정지
    own_open = defaultdict(lambda: defaultdict(int))  # 신규 등장 (diff 기반 개업)
    diff_months = set()

    for prev_m, cur_m in zip(snap_months, snap_months[1:]):
        prev, cur = snaps[prev_m], snaps[cur_m]
        diff_months.add(cur_m)
        for reg, info in prev.items():
            if info["cat"] == "closed":
                continue
            if reg not in cur:
                own_close[info["sido"]][cur_m] += 1
                own_close["전국"][cur_m] += 1
                continue
            pc, cc = info["cat"], cur[reg]["cat"]
            if cc == "closed" and pc != "closed":
                own_close[info["sido"]][cur_m] += 1
                own_close["전국"][cur_m] += 1
            elif cc == "pause" and pc == "active":
                s_pause[cur[reg]["sido"]][cur_m] += 1
                s_pause["전국"][cur_m] += 1
            elif cc == "ext" and pc != "ext":
                s_ext[cur[reg]["sido"]][cur_m] += 1
                s_ext["전국"][cur_m] += 1
            elif cc == "stop" and pc != "stop":
                s_stop[cur[reg]["sido"]][cur_m] += 1
                s_stop["전국"][cur_m] += 1
        for reg, info in cur.items():
            if reg not in prev and info["cat"] != "closed":
                own_open[info["sido"]][cur_m] += 1
                own_open["전국"][cur_m] += 1

    # ── 현재 상태 분포 (최신 스냅샷)
    status_now = defaultdict(lambda: {"active": 0, "pause": 0, "ext": 0, "stop": 0})
    for info in latest.values():
        cat = info["cat"]
        if cat == "closed":
            continue
        status_now[info["sido"]][cat] += 1
        status_now["전국"][cat] += 1

    # ── 표시 구간
    end_m = snap_months[-1]
    ey, em = int(end_m[:4]), int(end_m[5:7])
    sm = (ey * 12 + em - 1) - (MONTHS_WINDOW - 1)
    start_m = f"{sm // 12:04d}-{sm % 12 + 1:02d}"
    months = month_range(start_m, end_m)

    # ── 지역 목록
    sidos = set(reg_open) | set(status_now) | {s for (s, _) in backfill}
    sidos.discard("기타")
    regions = {}
    for sido in sorted(sidos):
        open_arr, close_arr, src_arr = [], [], []
        pause_arr, ext_arr, stop_arr = [], [], []
        for m in months:
            bf = backfill.get((sido, m), {})
            if m in diff_months:
                # 자체 집계 우선. diff 개업(스냅샷 신규 등장)과 등록일자 개업 중 큰 값 사용
                o = max(own_open[sido].get(m, 0), reg_open[sido].get(m, 0))
                open_arr.append(o)
                close_arr.append(own_close[sido].get(m, 0))
                src_arr.append("own")
                pause_arr.append(s_pause[sido].get(m, 0))
                ext_arr.append(s_ext[sido].get(m, 0))
                stop_arr.append(s_stop[sido].get(m, 0))
            elif bf.get("open") is not None or bf.get("close") is not None:
                open_arr.append(bf.get("open") if bf.get("open") is not None else reg_open[sido].get(m, 0))
                close_arr.append(bf.get("close"))
                src_arr.append("backfill")
                pause_arr.append(None)
                ext_arr.append(None)
                stop_arr.append(None)
            else:
                open_arr.append(reg_open[sido].get(m, 0))
                close_arr.append(None)
                src_arr.append("reg")
                pause_arr.append(None)
                ext_arr.append(None)
                stop_arr.append(None)
        st = status_now.get(sido, {"active": 0, "pause": 0, "ext": 0, "stop": 0})
        regions[sido] = {
            "open": open_arr, "close": close_arr, "source": src_arr,
            "pause": pause_arr, "ext": ext_arr, "stop": stop_arr,
            "active": st["active"],
            "status": {"영업중": st["active"], "휴업": st["pause"], "휴업연장": st["ext"], "업무정지": st["stop"]},
        }
    if "전국" in regions:
        regions = {"전국": regions.pop("전국"), **regions}

    payload = {
        "updated": date.today().isoformat(),
        "latest_snapshot": end_m,
        "sample": False,
        "months": months,
        "regions": regions,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"완료 → {OUT_FILE} (기간 {months[0]} ~ {months[-1]}, 지역 {len(regions)}개)")
    if len(snap_months) < 2 and not backfill:
        print("※ 스냅샷 1개 + backfill 없음: 폐업·영업중단 수치는 다음 달 스냅샷부터 집계됩니다.")


if __name__ == "__main__":
    sys.exit(main())
