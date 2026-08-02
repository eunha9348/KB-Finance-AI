"""서울 열린데이터광장 실거래가 API.

  OA-21275 부동산 실거래가   → tbLnOpendataRtms
  OA-21276 부동산 전월세가   → tbLnOpendataRentV

요청은 http://openapi.seoul.go.kr:8088/{키}/json/{서비스}/{시작}/{끝}/ 형태고
응답은 {서비스명: {RESULT: {...}, row: [...]}} 로 온다.

매매 엔드포인트가 ERROR-500 을 자주 뱉는다. 페이지를 작게 끊고 백오프 재시도를
넣어도 안 될 때가 있어서, 실패한 지표는 폴백을 유지하고 어떤 지표가 실데이터인지
provenance 로 같이 돌려준다. 컬럼명도 데이터셋마다 미묘하게 달라서 후보를 여러 개
두고 먼저 잡히는 걸 쓴다.
"""

import json
import time
import urllib.request
import urllib.error
from collections import defaultdict

BASE_URL = "http://openapi.seoul.go.kr:8088"
PAGE_SIZE = 300         # 크게 잡으면 500 이 더 자주 난다
TIMEOUT = 25            # GitHub Actions 처럼 해외 리전에서 부르면 느리다
MAX_RETRIES = 4
BACKOFF_BASE = 1.5      # 1.5, 3.0, 6.0초

SERVICE_RTMS = "tbLnOpendataRtms"      # 매매
SERVICE_RENT = "tbLnOpendataRentV"     # 전월세

FIELD_CANDIDATES = {
    "district_code": ["SGG_CD", "CGG_CD", "GU_CD"],
    "district_name": ["SGG_NM", "CGG_NM", "GU_NM"],
    "deal_amount":   ["THING_AMT", "OBJ_AMT", "MMB_AMT"],       # 매매 물건금액(만원)
    "deposit":       ["GRFE", "DEPOSIT"],                        # 전월세 보증금(만원)
    "monthly_rent":  ["RTFE", "MONTHLY_RENT"],                   # 월세(만원)
    "rent_type":     ["RENT_SE", "RENT_GBN"],                    # 전세/월세 구분
    "area":          ["ARCH_AREA", "RENT_AREA"],                 # 면적(㎡)
    "build_year":    ["ARCH_YR", "BLDG_YY"],
    "deal_date":     ["CTRT_DAY", "DCLR_YMD"],
    "dong_name":     ["STDG_NM", "BJDONG_NM"],
    "bldg_name":     ["BLDG_NM"],
}


class SeoulApiError(RuntimeError):
    """재시도해도 안 되는 오류. 호출부는 이걸 받으면 폴백을 쓴다."""


def _pick(row: dict, logical_field: str):
    for key in FIELD_CANDIDATES[logical_field]:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


class _Transient(Exception):
    """재시도하면 될 수도 있는 오류."""


def _raise_for_result(code, msg, raw=None):
    if not code or code.startswith("INFO-0"):
        return
    if code.startswith("ERROR-5") or "서버 오류" in (msg or ""):
        raise _Transient(f"{code}: {msg}")
    raise SeoulApiError(f"{code}: {msg or raw}")


def _request_once(api_key, service, start, end):
    url = f"{BASE_URL}/{api_key}/json/{service}/{start}/{end}/"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    body = payload.get(service)
    if not body:
        # 키가 승인 안 됐거나 서버가 죽으면 RESULT 가 최상위로 바로 온다
        top = payload.get("RESULT") or {}
        _raise_for_result(top.get("CODE", ""), top.get("MESSAGE", ""), raw=payload)
        raise SeoulApiError(f"예상치 못한 응답 형식: {list(payload.keys())}")
    _raise_for_result(body.get("RESULT", {}).get("CODE", ""),
                      body.get("RESULT", {}).get("MESSAGE", ""))
    return body.get("row", []), body.get("list_total_count", 0)


def _fetch_page(api_key, service, start, end):
    """페이지 하나 요청. 일시 오류면 백오프 두고 재시도."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return _request_once(api_key, service, start, end)
        except (_Transient, urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                print(f"[seoul_api] {service} {start}-{end} 일시 오류({e}) → "
                      f"{wait:.1f}s 후 재시도 ({attempt + 1}/{MAX_RETRIES - 1})")
                time.sleep(wait)
    raise SeoulApiError(f"{service} 재시도 소진: {last}")


def fetch_rows(api_key, service, max_rows=3000):
    """페이지 넘겨가며 max_rows 건까지 수집."""
    rows, start, total = [], 1, None
    while len(rows) < max_rows:
        end = min(start + PAGE_SIZE - 1, start + max_rows - len(rows) - 1)
        page, total = _fetch_page(api_key, service, start, end)
        if not page:
            break
        rows.extend(page)
        if total and len(rows) >= total:
            break
        start += PAGE_SIZE
    return rows


def fetch_district_averages(api_key, max_rows_each=3000, max_tx_store=800):
    """매매 + 전월세를 긁어서 자치구별 평균가를 낸다.

    (averages, transactions, provenance) 를 돌려주며 provenance 에 지표별
    실API 성공 여부와 에러 메시지가 들어있다.
    """
    sale_bucket = defaultdict(list)
    jeonse_bucket = defaultdict(list)
    names = {}
    unmatched_sample = None
    transactions = []
    prov = {"sale_from_api": False, "jeonse_from_api": False,
            "sale_error": None, "jeonse_error": None,
            "n_sale_rows": 0, "n_jeonse_rows": 0}

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # 매매
    try:
        rtms_rows = fetch_rows(api_key, SERVICE_RTMS, max_rows_each)
    except Exception as e:
        print(f"[seoul_api] 매매 실거래가 수집 실패: {e}")
        rtms_rows = []
        prov["sale_error"] = str(e)
    prov["n_sale_rows"] = len(rtms_rows)
    for row in rtms_rows:
        code, name = _pick(row, "district_code"), _pick(row, "district_name")
        amt = _num(_pick(row, "deal_amount"))
        if code and amt:
            sale_bucket[code].append(amt)
            names[code] = name or names.get(code)
            if len(transactions) < max_tx_store:
                transactions.append({
                    "district_code": code, "deal_type": "매매", "price": int(amt),
                    "monthly_rent": 0, "area_m2": _num(_pick(row, "area")),
                    "build_year": _num(_pick(row, "build_year")),
                    "deal_date": _pick(row, "deal_date"),
                    "raw_ref": _pick(row, "dong_name") or _pick(row, "bldg_name"),
                })
        elif unmatched_sample is None:
            unmatched_sample = list(row.keys())

    # 전월세. 월세도 적재는 하되 전세가율 계산엔 전세만 쓴다
    try:
        rent_rows = fetch_rows(api_key, SERVICE_RENT, max_rows_each)
    except Exception as e:
        print(f"[seoul_api] 전월세가 수집 실패: {e}")
        rent_rows = []
        prov["jeonse_error"] = str(e)
    prov["n_jeonse_rows"] = len(rent_rows)
    for row in rent_rows:
        code, name = _pick(row, "district_code"), _pick(row, "district_name")
        deposit = _num(_pick(row, "deposit"))
        monthly = _num(_pick(row, "monthly_rent")) or 0
        rent_type = (_pick(row, "rent_type") or "").strip()
        is_jeonse = (not rent_type) or ("전세" in rent_type)
        if code and deposit:
            names[code] = name or names.get(code)
            if is_jeonse:
                jeonse_bucket[code].append(deposit)
            if len(transactions) < max_tx_store:
                transactions.append({
                    "district_code": code,
                    "deal_type": "전세" if is_jeonse else "월세",
                    "price": int(deposit), "monthly_rent": int(monthly),
                    "area_m2": _num(_pick(row, "area")),
                    "build_year": _num(_pick(row, "build_year")),
                    "deal_date": _pick(row, "deal_date"),
                    "raw_ref": _pick(row, "dong_name") or _pick(row, "bldg_name"),
                })
        elif unmatched_sample is None:
            unmatched_sample = list(row.keys())

    prov["sale_from_api"] = bool(sale_bucket)
    prov["jeonse_from_api"] = bool(jeonse_bucket)

    if not sale_bucket and not jeonse_bucket:
        sample_msg = f" (샘플 응답 키: {unmatched_sample})" if unmatched_sample else ""
        raise SeoulApiError(
            "응답에서 컬럼을 하나도 매칭하지 못했습니다. "
            "FIELD_CANDIDATES 를 실제 API 응답 키로 갱신하세요." + sample_msg)

    def _std(xs):
        if len(xs) < 2:
            return None
        m = sum(xs) / len(xs)
        return round((sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5, 1)

    averages = {}
    codes = set(sale_bucket) | set(jeonse_bucket)
    for code in codes:
        sales = sale_bucket.get(code, [])
        jeonses = jeonse_bucket.get(code, [])
        averages[code] = {
            "name": names.get(code),
            "avg_sale": round(sum(sales) / len(sales)) if sales else None,
            "n_sale": len(sales),
            "avg_jeonse": round(sum(jeonses) / len(jeonses)) if jeonses else None,
            "n_jeonse": len(jeonses),
            "jeonse_std": _std(jeonses),   # 위험도 컨텍스트의 변동성 항에 쓰인다
        }
    return averages, transactions, prov


if __name__ == "__main__":
    import os
    key = os.environ.get("SEOUL_API_KEY")
    if not key:
        print("SEOUL_API_KEY 환경변수를 설정하세요.")
    else:
        averages, txs, prov = fetch_district_averages(key, max_rows_each=600)
        print("provenance:", prov)
        for code, v in sorted(averages.items()):
            print(code, v)
        print(f"수집된 원본 거래 {len(txs)}건")
