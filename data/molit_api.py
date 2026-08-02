"""국토교통부 실거래가 API (공공데이터포털 data.go.kr).

서울 열린데이터광장 매매 쪽이 8088 포트에서 ERROR-500 을 하도 뱉어서 대안으로 붙였다.
이쪽은 apis.data.go.kr 게이트웨이라 훨씬 안정적이라 MOLIT_API_KEY 가 있으면 이걸 먼저 쓴다.

  15126469 아파트 매매 실거래가   → RTMSDataSvcAptTradeDev
  15126474 아파트 전월세 실거래가 → RTMSDataSvcAptRent

serviceKey / LAWD_CD(시군구 5자리) / DEAL_YMD(YYYYMM) 를 넘기면 XML 로 온다.
태그명이 한글(<거래금액>)일 때도 영문(<dealAmount>)일 때도 있어서 후보를 둘 다 걸어뒀다.
"""

import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict

TRADE_URL = "http://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
RENT_URL = "http://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
TIMEOUT = 25
MAX_RETRIES = 4
BACKOFF_BASE = 1.5
NUM_ROWS = 1000

FIELD_CANDIDATES = {
    "deal_amount": ["거래금액", "dealAmount"],       # 만원, 콤마 포함
    "deposit":     ["보증금액", "deposit"],
    "monthly":     ["월세금액", "monthlyRent"],
    "area":        ["전용면적", "excluUseAr"],
    "build_year":  ["건축년도", "buildYear"],
    "y":           ["년", "dealYear"],
    "m":           ["월", "dealMonth"],
    "d":           ["일", "dealDay"],
    "apt":         ["아파트", "aptNm"],
    "dong":        ["법정동", "umdNm"],
}


class MolitApiError(RuntimeError):
    pass


class _Transient(Exception):
    pass


def _get(url, params):
    # serviceKey 가 이미 URL 인코딩된 상태로 발급되므로 % 를 다시 안 건드린다
    q = urllib.parse.urlencode(params, safe="%")
    full = f"{url}?{q}"
    with urllib.request.urlopen(full, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _tag(el, logical):
    for cand in FIELD_CANDIDATES[logical]:
        found = el.find(cand)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return None


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fetch(url, key, lawd, ymd):
    """구 하나 x 월 하나 조회. item 엘리먼트 리스트를 돌려준다."""
    params = {"serviceKey": key, "LAWD_CD": lawd, "DEAL_YMD": ymd,
              "pageNo": "1", "numOfRows": str(NUM_ROWS)}
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            xml = _get(url, params)
            root = ET.fromstring(xml)
            code_el = root.find(".//resultCode")
            code = (code_el.text or "").strip() if code_el is not None else ""
            if code and code not in ("000", "00"):
                msg_el = root.find(".//resultMsg")
                msg = (msg_el.text or "").strip() if msg_el is not None else ""
                # 트래픽 초과(22)나 서버 오류는 재시도, 키 문제는 재시도해도 소용없다
                if code in ("22", "05") or "SERVER" in msg.upper():
                    raise _Transient(f"{code}: {msg}")
                raise MolitApiError(f"{code}: {msg}")
            return root.findall(".//item")
        except (_Transient, urllib.error.URLError, TimeoutError, OSError,
                ET.ParseError) as e:
            last = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
    raise MolitApiError(f"재시도 소진: {last}")


def fetch_district_averages(key, district_codes, months=None, max_tx_store=800):
    """자치구별 평균가 산출. 반환 형식은 seoul_api 와 맞춰뒀다.

    district_codes 는 [(법정동5자리, 자치구명), ...]. 실거래 신고에 30일 정도 여유가
    있어서 최근 한 달만 보면 표본이 너무 적다. 기본 4개월치를 합산한다.
    """
    if months is None:
        months = _recent_months(4)

    sale_bucket = defaultdict(list)
    jeonse_bucket = defaultdict(list)
    transactions = []
    unmatched = None
    prov = {"sale_from_api": False, "jeonse_from_api": False,
            "sale_error": None, "jeonse_error": None,
            "n_sale_rows": 0, "n_jeonse_rows": 0, "source": "molit",
            "period": f"{months[-1]}~{months[0]}"}

    def collect(url, is_sale):
        nonlocal unmatched
        rows = 0
        err = None
        for code, name in district_codes:
            for ymd in months:
                try:
                    items = _fetch(url, key, code, ymd)
                except Exception as e:
                    err = str(e)
                    continue
                for it in items:
                    if is_sale:
                        amt = _num(_tag(it, "deal_amount"))
                        if amt:
                            sale_bucket[code].append(amt)
                            rows += 1
                            if len(transactions) < max_tx_store:
                                transactions.append(_tx(code, "매매", amt, 0, it))
                        elif unmatched is None:
                            unmatched = [c.tag for c in it]
                    else:
                        dep = _num(_tag(it, "deposit"))
                        mon = _num(_tag(it, "monthly")) or 0
                        if dep:
                            if mon == 0:      # 월세 0 이면 전세
                                jeonse_bucket[code].append(dep)
                            rows += 1
                            if len(transactions) < max_tx_store:
                                transactions.append(
                                    _tx(code, "전세" if mon == 0 else "월세", dep, mon, it))
                        elif unmatched is None:
                            unmatched = [c.tag for c in it]
        return rows, err

    prov["n_sale_rows"], prov["sale_error"] = collect(TRADE_URL, True)
    prov["n_jeonse_rows"], prov["jeonse_error"] = collect(RENT_URL, False)
    prov["sale_from_api"] = bool(sale_bucket)
    prov["jeonse_from_api"] = bool(jeonse_bucket)

    if not sale_bucket and not jeonse_bucket:
        raise MolitApiError(
            "응답에서 값을 하나도 얻지 못했습니다. 키 활용신청/필드명을 확인하세요."
            + (f" (샘플 태그: {unmatched})" if unmatched else ""))

    averages = {}
    names = dict(district_codes)
    for code in set(sale_bucket) | set(jeonse_bucket):
        sales = sale_bucket.get(code, [])
        jeonses = jeonse_bucket.get(code, [])
        averages[code] = {
            "name": names.get(code),
            "avg_sale": round(sum(sales) / len(sales)) if sales else None,
            "n_sale": len(sales),
            "avg_jeonse": round(sum(jeonses) / len(jeonses)) if jeonses else None,
            "n_jeonse": len(jeonses),
            "jeonse_std": _std(jeonses),
        }
    return averages, transactions, prov


def _tx(code, deal_type, price, monthly, it):
    y, m, d = _tag(it, "y"), _tag(it, "m"), _tag(it, "d")
    date = f"{y}-{int(m):02d}-{int(d):02d}" if (y and m and d) else None
    return {"district_code": code, "deal_type": deal_type, "price": int(price),
            "monthly_rent": int(monthly), "area_m2": _num(_tag(it, "area")),
            "build_year": _num(_tag(it, "build_year")), "deal_date": date,
            "raw_ref": _tag(it, "apt") or _tag(it, "dong")}


def _std(xs):
    if len(xs) < 2:
        return None
    mu = sum(xs) / len(xs)
    return round((sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5, 1)


def _recent_months(n):
    import datetime as dt
    today = dt.date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        out.append(f"{y}{m:02d}")
    return out


if __name__ == "__main__":
    import os
    key = os.environ.get("MOLIT_API_KEY")
    if not key:
        print("MOLIT_API_KEY 환경변수를 설정하세요.")
    else:
        codes = [("11680", "강남구"), ("11620", "관악구"), ("11500", "강서구")]
        avg, txs, prov = fetch_district_averages(key, codes, months=_recent_months(3))
        print("provenance:", prov)
        for c, v in avg.items():
            print(c, v)
        print(f"원본 거래 {len(txs)}건")
