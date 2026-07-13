#!/usr/bin/env python3
"""
부동산중개업 월별 개업/폐업 집계 스크립트

사용법:
  1. 브이월드(vworld.kr) 또는 공공데이터포털에서 전국 부동산중개업 CSV를 다운로드
  2. snapshots/ 폴더에 "YYYY-MM.csv" 형식으로 저장 (예: snapshots/2026-07.csv)
  3. python aggregate.py 실행 → data.json 생성

집계 방식:
  - 개업: 각 사무소의 '개설등록일자'를 월 단위로 집계
  - 폐업: 연속된 두 스냅샷을 비교(diff)하여,
          이전 달에 있던 등록번호가 사라졌거나 상태가 '폐업'으로 바뀐 경우로 집계
          → 스냅샷이 2개 이상 쌓여야 폐업 수치가 나오기 시작합니다.
"""

import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

SNAP_DIR = Path("snapshots")
OUT_FILE = Path("data.json")
MONTHS_WINDOW = 36  # 대시보드에 표시할 최근 개월 수

# 데이터 출처(브이월드/공공데이터포털/지자체)에 따라 컬럼명이 조금씩 다르므로
# 흔한 이름들을 후보로 두고 자동 탐색합니다. 안 맞으면 여기에 추가하세요.
REGNO_CANDIDATES = ["개설등록번호", "등록번호", "중개업등록번호"]
OPENDATE_CANDIDATES = ["개설등록일자", "등록일자", "개설등록일"]
ADDR_CANDIDATES = ["소재지도로명주소", "소재지지번주소", "도로명주소", "지번주소", "소재지", "주소"]
STATUS_CANDIDATES = ["영업상태", "영업상태구분", "상태구분", "영업상태명"]

CLOSED_KEYWORDS = ("폐업", "말소", "취소")

SIDO_NORMALIZE = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "강원도": "강원특별자치도",
    "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도", "전라북도": "전북특별자치도",
    "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도", "제주도": "제주특별자치도",
}


def read_csv_rows(path: Path) -> list[dict]:
    """인코딩(utf-8/cp949)을 자동 판별해 CSV를 읽는다."""
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
    # 부분 일치 (예: '개설등록일자(YYYYMMDD)')
    for cand in candidates:
        for k in keys:
            if cand in k:
                return keys[k]
    return None


def parse_month(raw: str) -> str | None:
    """'2024-03-15', '20240315', '2024.3.15' 등에서 'YYYY-MM'을 뽑는다."""
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) >= 6:
        y, m = digits[:4], digits[4:6]
        if "1900" <= y <= "2100" and "01" <= m <= "12":
            return f"{y}-{m}"
    return None


def parse_sido(addr: str) -> str:
    if not addr:
        return "기타"
    token = addr.strip().split()[0] if addr.strip() else "기타"
    return SIDO_NORMALIZE.get(token, token)


def load_snapshot(path: Path) -> dict[str, dict]:
    """등록번호 → {open_month, sido, closed} 로 정규화."""
    rows = read_csv_rows(path)
    first = rows[0]
    c_reg = pick_col(first, REGNO_CANDIDATES)
    c_open = pick_col(first, OPENDATE_CANDIDATES)
    c_addr = pick_col(first, ADDR_CANDIDATES)
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
            "sido": parse_sido(r.get(c_addr, "")) if c_addr else "기타",
            "closed": any(k in status for k in CLOSED_KEYWORDS),
        }
    return out


def month_range(start: str, end: str) -> list[str]:
    ys, ms = int(start[:4]), int(start[5:7])
    ye, me = int(end[:4]), int(end[5:7])
    out = []
    while (ys, ms) <= (ye, me):
        out.append(f"{ys:04d}-{ms:02d}")
        ms += 1
        if ms == 13:
            ys, ms = ys + 1, 1
    return out


def main():
    files = sorted(SNAP_DIR.glob("*.csv"))
    files = [f for f in files if re.fullmatch(r"\d{4}-\d{2}", f.stem)]
    if not files:
        raise SystemExit(
            "[안내] snapshots/ 폴더에 YYYY-MM.csv 형식의 파일이 없습니다.\n"
            "       예: snapshots/2026-07.csv"
        )

    print(f"스냅샷 {len(files)}개 발견: {[f.stem for f in files]}")
    snaps = {f.stem: load_snapshot(f) for f in files}
    snap_months = sorted(snaps.keys())
    latest = snaps[snap_months[-1]]

    # ── 개업 집계: 모든 스냅샷을 합쳐 등록번호별 개설월을 확보 (생존편향 완화)
    registry: dict[str, dict] = {}
    for m in snap_months:
        for reg, info in snaps[m].items():
            if reg not in registry or (info["open_month"] and not registry[reg]["open_month"]):
                registry[reg] = info

    open_cnt = defaultdict(lambda: defaultdict(int))  # sido -> month -> n
    for info in registry.values():
        om = info["open_month"]
        if om:
            open_cnt[info["sido"]][om] += 1
            open_cnt["전국"][om] += 1

    # ── 폐업 집계: 연속 스냅샷 diff
    close_cnt = defaultdict(lambda: defaultdict(int))
    close_months_available = set()
    for prev_m, cur_m in zip(snap_months, snap_months[1:]):
        prev, cur = snaps[prev_m], snaps[cur_m]
        close_months_available.add(cur_m)
        for reg, info in prev.items():
            if info["closed"]:
                continue  # 이미 폐업 상태였던 곳은 제외
            gone = reg not in cur
            became_closed = (not gone) and cur[reg]["closed"]
            if gone or became_closed:
                sido = info["sido"]
                close_cnt[sido][cur_m] += 1
                close_cnt["전국"][cur_m] += 1

    # ── 영업중 사무소 수 (최신 스냅샷 기준)
    active = defaultdict(int)
    for info in latest.values():
        if not info["closed"]:
            active[info["sido"]] += 1
            active["전국"] += 1

    # ── 표시 구간: 최신 스냅샷 월 기준 최근 MONTHS_WINDOW개월
    end_m = snap_months[-1]
    ey, em = int(end_m[:4]), int(end_m[5:7])
    sm = (ey * 12 + em - 1) - (MONTHS_WINDOW - 1)
    start_m = f"{sm // 12:04d}-{sm % 12 + 1:02d}"
    months = month_range(start_m, end_m)

    regions = {}
    for sido in sorted(set(list(open_cnt.keys()) + list(close_cnt.keys()))):
        regions[sido] = {
            "open": [open_cnt[sido].get(m, 0) for m in months],
            "close": [
                close_cnt[sido].get(m, 0) if m in close_months_available else None
                for m in months
            ],
            "active": active.get(sido, 0),
        }
    # 전국을 맨 앞으로
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
    if len(snap_months) < 2:
        print("※ 스냅샷이 1개뿐이라 폐업 수치는 아직 비어 있습니다. 다음 달 파일부터 집계됩니다.")


if __name__ == "__main__":
    sys.exit(main())
