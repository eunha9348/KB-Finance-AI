"""국토부 실거래가 공개시스템(rt.molit.go.kr) CSV 로더.

거기서 지역·기간·거래유형 골라서 CSV 를 바로 받을 수 있다. 인증키가 필요없고
단지명·보증금·전용면적·계약일이 전부 들어있어서, API 키 없이 실제 매물로
서비스를 채우는 가장 빠른 방법이다.

파일은 CP949 로 내려오고 맨 위에 검색조건 몇 줄이 붙어있다가 헤더가 나온다.
전월세는 시군구/단지명/전월세구분/전용면적/보증금(만원)/월세금(만원)/층/건축년도,
매매는 시군구/단지명/전용면적/거래금액(만원)/층/건축년도 식.
"""

import csv
import io
import re
from pathlib import Path

# 헤더에 이 토큰이 '포함'되면 그 컬럼으로 잡는다. 선언 순서가 곧 우선순위인데,
# '전월세구분'에 '월세'가 들어있어서 rent_type 을 monthly 보다 먼저 둬야 한다.
HEADER_TOKENS = {
    "sigungu":    ["시군구"],
    "complex":    ["단지명", "건물명"],
    "rent_type":  ["전월세구분"],
    "area":       ["전용면적"],
    "amount":     ["거래금액"],
    "deposit":    ["보증금"],          # '종전계약 보증금'보다 앞에 있는 걸 먼저 잡게
    "monthly":    ["월세금", "월세"],
    "ym":         ["계약년월"],
    "day":        ["계약일"],
    "build_year": ["건축년도", "건축연도"],
    "floor":      ["층"],
    "road":       ["도로명"],
}


def _decode(raw: bytes) -> str:
    for enc in ("cp949", "euc-kr", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _num(v):
    if v is None:
        return None
    s = re.sub(r"[,\s]", "", str(v))
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find_header(rows):
    # 시군구랑 단지명(또는 금액)이 같이 있는 첫 줄을 헤더로 본다
    for i, row in enumerate(rows):
        cells = [c.strip() for c in row]
        joined = " ".join(cells)
        if any("시군구" in c for c in cells) and (
                "단지명" in joined or "거래금액" in joined or "보증금" in joined):
            return i
    return None


def _colmap(header):
    hs = [h.strip() for h in header]
    m = {}
    used = set()   # 한 컬럼이 두 필드에 중복으로 잡히지 않게
    for field, tokens in HEADER_TOKENS.items():
        for idx, h in enumerate(hs):
            if idx in used:
                continue
            if any(tok in h for tok in tokens):
                m[field] = idx
                used.add(idx)
                break
    return m


_GU = re.compile(r"([가-힣]+(?:구|군|시))")


def _district_from_sigungu(s):
    """'서울특별시 강남구 역삼동' → '강남구'."""
    if not s:
        return None
    toks = _GU.findall(s)
    for t in toks:
        if t.endswith("구"):
            return t
    return toks[-1] if toks else None


def load_transactions(csv_path, allowed_districts=None):
    """CSV 한 개를 거래 dict 리스트로. allowed_districts 를 주면 그 구만 걸러낸다."""
    raw = Path(csv_path).read_bytes()
    text = _decode(raw)
    rows = list(csv.reader(io.StringIO(text)))
    hidx = _find_header(rows)
    if hidx is None:
        return []
    cmap = _colmap(rows[hidx])
    out = []

    def cell(row, field):
        i = cmap.get(field)
        return row[i].strip() if (i is not None and i < len(row)) else None

    for row in rows[hidx + 1:]:
        if not row or all(not c.strip() for c in row):
            continue
        dist = _district_from_sigungu(cell(row, "sigungu"))
        if not dist:
            continue
        if allowed_districts is not None and dist not in allowed_districts:
            continue
        area = _num(cell(row, "area"))
        by = _num(cell(row, "build_year"))
        ym = cell(row, "ym")
        day = cell(row, "day")
        date = None
        if ym and len(re.sub(r"\D", "", ym)) >= 6:
            ymd = re.sub(r"\D", "", ym)[:6]
            d2 = re.sub(r"\D", "", day or "")[:2].rjust(2, "0") if day else "01"
            date = f"{ymd[:4]}-{ymd[4:6]}-{d2}"
        comp = cell(row, "complex")
        floor = cell(row, "floor")
        road = cell(row, "road")

        amount = _num(cell(row, "amount"))       # 매매 파일
        deposit = _num(cell(row, "deposit"))     # 전월세 파일
        if amount:
            out.append({"district_name": dist, "complex": comp, "deal_type": "매매",
                        "price": int(amount), "monthly": 0, "area_m2": area,
                        "build_year": int(by) if by else None, "deal_date": date,
                        "floor": floor, "road": road})
        elif deposit:
            rt = cell(row, "rent_type") or ""
            monthly = _num(cell(row, "monthly")) or 0
            dtype = "전세" if (monthly == 0 and "월세" not in rt) else "월세"
            out.append({"district_name": dist, "complex": comp, "deal_type": dtype,
                        "price": int(deposit), "monthly": int(monthly), "area_m2": area,
                        "build_year": int(by) if by else None, "deal_date": date,
                        "floor": floor, "road": road})
    return out


def load_dir(data_dir, allowed_districts=None):
    """data_dir 의 realprice*.csv 를 전부 읽어 합친다."""
    data_dir = Path(data_dir)
    files = sorted(data_dir.glob("realprice*.csv"))
    txs = []
    for f in files:
        try:
            txs.extend(load_transactions(f, allowed_districts))
        except Exception as e:
            print(f"[realprice_csv] {f.name} 읽기 실패: {e}")
    return txs, [f.name for f in files]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        txs = load_transactions(sys.argv[1])
        print(f"거래 {len(txs)}건")
        for t in txs[:5]:
            print(t)
