"""공공데이터를 읽어 db/housing.db 를 만드는 ETL.

데이터 소스는 세 갈래이고 위에서부터 되는 걸 쓴다.
  1. data/realprice*.csv  — rt.molit.go.kr 에서 받은 실거래가 CSV. 실제 단지가 나온다.
  2. MOLIT_API_KEY / SEOUL_API_KEY — 자치구 평균 시세만 실거래로 갱신
  3. 아무것도 없으면 아래 DISTRICTS 의 근사 시세로 시연용 샘플 생성

키는 매번 붙이기 귀찮으면 .env 에 넣어두면 된다(.env.example 참고).

    python data/etl.py
    MOLIT_API_KEY=... python data/etl.py
"""

import os
import sqlite3
import random
from pathlib import Path

from risk_engine import RiskInput, assess
from kb_products import PRODUCTS, match_product
import seoul_api
import molit_api
import realprice_csv
import news_sentiment

from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "housing.db"
SCHEMA_PATH = ROOT / "db" / "schema.sql"


def _load_dotenv(path: Path):
    """.env 의 KEY=VALUE 를 환경변수로. 이미 있는 값은 안 덮어쓴다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")

random.seed(42)  # 돌릴 때마다 같은 결과가 나오게

# 자치구 중심 좌표 + 폴백 평균시세(만원, 전용 60㎡ 환산 근사).
# 시세는 API/CSV 가 없을 때만 쓰이고, 있으면 실거래 평균으로 덮어쓴다.
DISTRICTS = [
    # code,   name,     lat,      lng,      avg_sale, avg_jeonse
    ("11680", "강남구", 37.5172, 127.0473, 145000, 78000),
    ("11650", "서초구", 37.4837, 127.0324, 138000, 74000),
    ("11710", "송파구", 37.5145, 127.1060, 110000, 62000),
    ("11440", "마포구", 37.5663, 126.9019,  92000, 55000),
    ("11170", "용산구", 37.5326, 126.9905, 118000, 60000),
    ("11215", "광진구", 37.5385, 127.0823,  82000, 50000),
    ("11290", "성북구", 37.5894, 127.0167,  68000, 43000),
    ("11305", "강북구", 37.6396, 127.0257,  52000, 35000),
    ("11500", "강서구", 37.5509, 126.8495,  70000, 44000),
    ("11470", "양천구", 37.5169, 126.8664,  85000, 50000),
    ("11530", "구로구", 37.4954, 126.8874,  62000, 40000),
    ("11545", "금천구", 37.4569, 126.8955,  58000, 38000),
    ("11620", "관악구", 37.4784, 126.9516,  60000, 41000),   # 대학가·청년 밀집
    ("11560", "영등포구", 37.5264, 126.8962, 78000, 48000),
    ("11350", "노원구", 37.6542, 127.0568,  55000, 37000),   # 대학가
    ("11380", "은평구", 37.6027, 126.9291,  62000, 41000),
    ("11110", "종로구", 37.5735, 126.9790,  88000, 52000),
    ("11140", "중구",   37.5636, 126.9976,  90000, 53000),
    ("11230", "동대문구", 37.5744, 127.0396, 66000, 44000),  # 대학가
    ("11260", "중랑구", 37.6063, 127.0925,  52000, 36000),
    ("11320", "도봉구", 37.6688, 127.0471,  50000, 34000),
    ("11410", "서대문구", 37.5791, 126.9368, 74000, 47000),  # 대학가(신촌)
    ("11590", "동작구", 37.5124, 126.9393,  76000, 47000),
    ("11740", "강동구", 37.5301, 127.1238,  84000, 51000),
    ("11200", "성동구", 37.5634, 127.0369,  90000, 54000),
]

BUILDING_TYPES = ["오피스텔", "다세대", "빌라", "도시형생활주택", "아파트"]

# 실거래가 평균은 아파트 위주로 잡히는데 청년들이 실제 사는 건 오피스텔·빌라다.
# 같은 구 안에서도 가격대가 확 다르니까 유형별로 배율을 걸어서 낮춰준다.
BUILDING_TYPE_PRICE_FACTOR = {
    "오피스텔": 0.55,
    "다세대": 0.42,
    "빌라": 0.38,
    "도시형생활주택": 0.48,
    "아파트": 1.0,
}


def init_db(conn):
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_districts(conn):
    conn.executemany(
        "INSERT OR REPLACE INTO districts "
        "(district_code, name, lat, lng, avg_sale_price, avg_jeonse) "
        "VALUES (?,?,?,?,?,?)", DISTRICTS
    )


def load_finance_products(conn):
    cols = ["product_code", "name", "provider", "product_type", "guarantee_agency",
            "rate_min", "rate_max", "rate_asof", "max_loan_manwon", "max_deposit_manwon",
            "target_grade", "description", "reference_url"]
    rows = [tuple(p[c] for c in cols) for p in PRODUCTS.values()]
    placeholders = ",".join("?" * len(cols))
    conn.executemany(
        f"INSERT OR REPLACE INTO finance_products ({','.join(cols)}) VALUES ({placeholders})",
        rows,
    )


def apply_real_seoul_data(conn):
    """실거래가 API 로 자치구 평균 시세를 갱신. 실패하면 폴백 값 그대로 둔다."""
    molit_key = os.environ.get("MOLIT_API_KEY")
    seoul_key = os.environ.get("SEOUL_API_KEY")
    averages = transactions = prov = None
    used_source = None

    # 국토부 쪽이 훨씬 안정적이라 먼저 시도하고, 안 되면 서울 열린데이터광장으로
    if molit_key:
        print("[etl] MOLIT_API_KEY 감지 → 국토부 data.go.kr 실거래가 API 호출 시도")
        try:
            codes = [(c, n) for c, n, *_ in DISTRICTS]
            averages, transactions, prov = molit_api.fetch_district_averages(molit_key, codes)
            used_source = "국토부(data.go.kr)"
        except Exception as e:
            print(f"[etl] 국토부 API 실패: {e}")
            averages = None
    if averages is None and seoul_key:
        print("[etl] 서울 열린데이터광장 실거래가 API 호출 시도")
        try:
            averages, transactions, prov = seoul_api.fetch_district_averages(seoul_key)
            used_source = "서울 열린데이터광장"
        except Exception as e:
            print(f"[etl] 서울 API 실패: {e}")
            averages = None
    if averages is None:
        if not molit_key and not seoul_key:
            print("[etl] MOLIT_API_KEY/SEOUL_API_KEY 미설정 → FALLBACK 평균시세 사용")
        else:
            print("[etl] 모든 실거래가 API 실패 → FALLBACK 평균시세 유지")
        return {"sale": False, "jeonse": False}
    print(f"[etl] 실데이터 소스: {used_source}")

    # 매매만 성공하고 전세는 실패하는 경우가 흔해서, 지표별로 따로 출처를 남긴다.
    sale_ok = prov.get("sale_from_api")
    jeonse_ok = prov.get("jeonse_from_api")
    real_src = "molit_api" if prov.get("source") == "molit" else "seoul_open_data"
    updated = 0
    for code, agg in averages.items():
        row = conn.execute(
            "SELECT avg_sale_price, avg_jeonse FROM districts WHERE district_code=?",
            (code,)).fetchone()
        if row is None:
            continue  # 서울 외 지역 코드
        new_sale = agg["avg_sale"] if (sale_ok and agg["avg_sale"]) else row[0]
        new_jeonse = agg["avg_jeonse"] if (jeonse_ok and agg["avg_jeonse"]) else row[1]
        cv = 0.0  # 전세가 변동계수 = 표준편차/평균
        if jeonse_ok and agg.get("jeonse_std") and agg.get("avg_jeonse"):
            cv = round(agg["jeonse_std"] / agg["avg_jeonse"], 4)
        conn.execute(
            "UPDATE districts SET avg_sale_price=?, avg_jeonse=?, "
            "sale_source=?, jeonse_source=?, jeonse_cv=? WHERE district_code=?",
            (new_sale, new_jeonse,
             real_src if (sale_ok and agg["avg_sale"]) else "fallback",
             real_src if (jeonse_ok and agg["avg_jeonse"]) else "fallback",
             cv, code))
        updated += 1

    tx_source = "molit_api" if prov.get("source") == "molit" else "seoul_open_data"
    for tx in transactions:
        if not conn.execute("SELECT 1 FROM districts WHERE district_code=?",
                            (tx["district_code"],)).fetchone():
            continue
        conn.execute(
            """INSERT INTO transactions
               (district_code, deal_type, price, monthly_rent, area_m2, build_year,
                deal_date, raw_ref, source)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tx["district_code"], tx["deal_type"], tx["price"], tx["monthly_rent"],
             tx["area_m2"], tx["build_year"], tx["deal_date"], tx["raw_ref"], tx_source))

    src_msg = (f"매매={'실API' if sale_ok else 'FALLBACK'}, "
               f"전세={'실API' if jeonse_ok else 'FALLBACK'}")
    if prov.get("sale_error"):
        src_msg += f" (매매 오류: {prov['sale_error']})"
    print(f"[etl] 실거래가 반영: 자치구 {updated}개 갱신 [{src_msg}], "
          f"원본 거래 {len(transactions)}건 적재")
    return {"sale": bool(sale_ok), "jeonse": bool(jeonse_ok)}


def apply_news_sentiment(conn):
    """뉴스 감성 점수를 자치구에 반영. 코퍼스가 파일로 들어있어 키 없이도 돈다."""
    analysis = news_sentiment.analyze()
    names = [name for _, name, *_ in DISTRICTS]
    scores = news_sentiment.all_district_scores(names)
    for name, s in scores.items():
        note = ("근거 %d건: %s" % (len(s["evidence"]), s["note"])) if not s["inherited"] \
            else "서울 기준선 상속(자치구 실명 근거 없음)"
        conn.execute("UPDATE districts SET news_sentiment=?, sentiment_note=? WHERE name=?",
                     (s["score"], note, name))
    print(f"[etl] 뉴스 감성분석 반영: 서울 기준선 {analysis['city_index']} "
          f"(기사 {analysis['n_articles']}건, risk_mass {analysis['risk_mass']}), "
          f"자치구 실명근거 {sum(1 for s in scores.values() if not s['inherited'])}곳")
    return analysis


def generate_properties(conn, per_district=12):
    """CSV 가 없을 때 쓰는 시연용 매물 생성. 자치구 평균에서 산포시킨다."""
    rows = conn.execute("SELECT district_code, name, lat, lng, avg_sale_price, "
                        "avg_jeonse, jeonse_cv, news_sentiment FROM districts").fetchall()
    city_baseline = news_sentiment.analyze()["city_index"]
    prop_count = 0
    for code, name, lat, lng, avg_sale, avg_jeonse, jeonse_cv, sentiment in rows:
        for i in range(per_district):
            # 구 중심에서 0.02도쯤 흩뿌린다
            plat = round(lat + random.uniform(-0.018, 0.018), 6)
            plng = round(lng + random.uniform(-0.022, 0.022), 6)
            btype = random.choices(BUILDING_TYPES, weights=[30, 25, 20, 15, 10])[0]
            area = round(random.uniform(24, 59), 1)
            build_year = random.randint(1998, 2022)

            sale_price = int(avg_sale * BUILDING_TYPE_PRICE_FACTOR[btype] * random.uniform(0.7, 1.3))
            # 전세가율은 그 구의 실제 전세가율을 중심으로 뽑는다.
            # 이래야 API 로 갱신된 실데이터가 개별 매물 보증금까지 전파된다.
            if avg_jeonse and avg_sale:
                base_ratio = min(max(avg_jeonse / avg_sale, 0.45), 0.85)
            else:
                base_ratio = 0.62
            if btype != "아파트":
                base_ratio += 0.06   # 비아파트는 전세가율이 더 높게 붙는다
            jeonse_ratio = base_ratio * random.uniform(0.9, 1.15)
            if random.random() < 0.12:            # 깡통전세 꼬리도 좀 섞어야 등급이 갈린다
                jeonse_ratio = random.uniform(0.95, 1.15)
            jeonse_ratio = min(jeonse_ratio, 1.25)
            deposit = int(sale_price * jeonse_ratio)
            # 근저당은 등기부 연동 전이라 0~60% 사이로 추정
            mortgage = int(sale_price * random.choices(
                [0, random.uniform(0.05, 0.25), random.uniform(0.25, 0.6)],
                weights=[35, 40, 25])[0])
            is_illegal = 1 if random.random() < 0.08 else 0

            cur = conn.execute(
                """INSERT INTO properties
                   (district_code, address, lat, lng, building_type, area_m2,
                    build_year, sale_price, deposit, mortgage_amount, is_illegal, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, 'partner_agency')""",
                (code, f"{name} {btype} {i+1}호", plat, plng, btype, area,
                 build_year, sale_price, deposit, mortgage, is_illegal))
            pid = cur.lastrowid

            r = assess(RiskInput(
                sale_price, deposit, mortgage, bool(is_illegal),
                building_type=btype, build_year=build_year,
                district_jeonse_cv=jeonse_cv or 0.0,
                district_sentiment=sentiment or 0.0,
                city_sentiment_baseline=city_baseline))
            m = match_product(r.risk_score, r.risk_grade, r.jeonse_ratio,
                               r.senior_debt_ratio, deposit)
            conn.execute(
                """INSERT INTO risk_assessments
                   (property_id, jeonse_ratio, senior_debt_ratio,
                    fundamental_score, context_score, risk_score,
                    risk_grade, recommended_product, proposal_rate_adjust, match_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (pid, r.jeonse_ratio, r.senior_debt_ratio,
                 r.fundamental_score, r.context_score, r.risk_score,
                 r.risk_grade, m.product_code, m.proposal_rate_adjust, m.match_reason))
            prop_count += 1
    return prop_count


def apply_real_csv(conn, txs):
    """CSV 실거래로 자치구 평균·변동성 갱신 + 원본 거래 적재."""
    names = {name for _, name, *_ in DISTRICTS}
    code_of = {name: code for code, name, *_ in DISTRICTS}
    sale_by, jeonse_by = defaultdict(list), defaultdict(list)
    for t in txs:
        if t["district_name"] not in names:
            continue
        if t["deal_type"] == "매매":
            sale_by[t["district_name"]].append(t["price"])
        elif t["deal_type"] == "전세":
            jeonse_by[t["district_name"]].append(t["price"])

    def _std(xs):
        if len(xs) < 2:
            return None
        mu = sum(xs) / len(xs)
        return (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5

    for name in names:
        code = code_of[name]
        cur = conn.execute("SELECT avg_sale_price, avg_jeonse FROM districts WHERE district_code=?",
                           (code,)).fetchone()
        sales, jeonses = sale_by.get(name, []), jeonse_by.get(name, [])
        new_sale = round(sum(sales) / len(sales)) if sales else cur[0]
        new_jeonse = round(sum(jeonses) / len(jeonses)) if jeonses else cur[1]
        cv = 0.0
        if jeonses and new_jeonse:
            s = _std(jeonses)
            cv = round(s / new_jeonse, 4) if s else 0.0
        conn.execute(
            "UPDATE districts SET avg_sale_price=?, avg_jeonse=?, sale_source=?, "
            "jeonse_source=?, jeonse_cv=? WHERE district_code=?",
            (new_sale, new_jeonse,
             "molit_csv" if sales else "fallback",
             "molit_csv" if jeonses else "fallback", cv, code))

    stored = 0
    for t in txs:
        code = code_of.get(t["district_name"])
        if not code:
            continue
        conn.execute(
            """INSERT INTO transactions
               (district_code, deal_type, price, monthly_rent, area_m2, build_year,
                deal_date, raw_ref, source)
               VALUES (?,?,?,?,?,?,?,?, 'molit_csv')""",
            (code, t["deal_type"], t["price"], t.get("monthly", 0), t.get("area_m2"),
             t.get("build_year"), t.get("deal_date"), t.get("complex")))
        stored += 1
    print(f"[etl] 국토부 CSV 실거래 {stored}건 적재 (매매 {sum(len(v) for v in sale_by.values())}, "
          f"전세 {sum(len(v) for v in jeonse_by.values())})")
    return {"sale": any(sale_by.values()), "jeonse": any(jeonse_by.values())}


def generate_real_properties(conn, txs, city_baseline, per_cap=30):
    """전세 실거래 한 건 = 매물 한 건. 단지명·보증금·면적이 전부 실제 값이다.

    매매가는 같은 단지(없으면 같은 구) 매매 실거래의 ㎡당 단가 중앙값 × 면적으로 추정.
    근저당은 등기부가 공공API에 없어서 미확인(debt_known=0)으로 둔다.
    """
    from statistics import median
    dctx = {name: (code, cv, sent) for code, name, cv, sent in conn.execute(
        "SELECT district_code, name, jeonse_cv, news_sentiment FROM districts")}
    coords = {name: (lat, lng) for name, lat, lng in conn.execute(
        "SELECT name, lat, lng FROM districts")}

    unit_complex, unit_district = defaultdict(list), defaultdict(list)
    for t in txs:
        if t["deal_type"] == "매매" and t.get("area_m2"):
            u = t["price"] / t["area_m2"]
            unit_complex[(t["district_name"], t["complex"])].append(u)
            unit_district[t["district_name"]].append(u)

    def est_sale(t):
        area = t.get("area_m2") or 0
        if not area:
            return None
        key = (t["district_name"], t["complex"])
        if unit_complex.get(key):
            u = median(unit_complex[key])
        elif unit_district.get(t["district_name"]):
            u = median(unit_district[t["district_name"]])
        else:
            return None
        return int(u * area)

    jeonse = [t for t in txs if t["deal_type"] == "전세"
              and t.get("area_m2") and t["district_name"] in dctx]
    random.shuffle(jeonse)
    counts, n = defaultdict(int), 0
    for t in jeonse:
        d = t["district_name"]
        if counts[d] >= per_cap:
            continue
        sale = est_sale(t)
        if not sale or sale <= 0:
            continue
        code, cv, sent = dctx[d]
        lat, lng = coords[d]
        plat = round(lat + random.uniform(-0.02, 0.02), 6)
        plng = round(lng + random.uniform(-0.025, 0.025), 6)
        area, by = t["area_m2"], t.get("build_year")
        comp = t["complex"] or "아파트"
        floor = t.get("floor")
        addr = f"{d} {comp}" + (f" 전용{area:.0f}㎡" if area else "") + \
               (f" {floor}층" if floor else "")
        deposit = t["price"]
        cur = conn.execute(
            """INSERT INTO properties
               (district_code, address, complex_name, lat, lng, building_type, area_m2,
                build_year, sale_price, deposit, mortgage_amount, is_illegal, debt_known, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0,'molit_csv')""",
            (code, addr, comp, plat, plng, "아파트", area, by, sale, deposit))
        pid = cur.lastrowid
        # 근저당을 모르니 선순위채권비율은 전세가율과 같아진다
        r = assess(RiskInput(sale, deposit, 0, False, "아파트", by,
                             district_jeonse_cv=cv or 0.0, district_sentiment=sent or 0.0,
                             city_sentiment_baseline=city_baseline))
        m = match_product(r.risk_score, r.risk_grade, r.jeonse_ratio,
                          r.senior_debt_ratio, deposit)
        conn.execute(
            """INSERT INTO risk_assessments
               (property_id, jeonse_ratio, senior_debt_ratio, fundamental_score,
                context_score, risk_score, risk_grade, recommended_product,
                proposal_rate_adjust, match_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (pid, r.jeonse_ratio, r.senior_debt_ratio, r.fundamental_score,
             r.context_score, r.risk_score, r.risk_grade, m.product_code,
             m.proposal_rate_adjust, m.match_reason))
        counts[d] += 1
        n += 1
    return n


def main():
    DB_PATH.parent.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        load_districts(conn)
        load_finance_products(conn)

        city_baseline = news_sentiment.analyze()["city_index"]
        district_names = {name for _, name, *_ in DISTRICTS}
        csv_txs, csv_files = realprice_csv.load_dir(ROOT / "data", district_names)
        if csv_txs:
            print(f"[etl] 실거래가 CSV 감지: {csv_files} → 실제 매물 모드")
            real = apply_real_csv(conn, csv_txs)
            apply_news_sentiment(conn)
            n = generate_real_properties(conn, csv_txs, city_baseline)
            mode = "실거래 CSV(실제 매물)"
        else:
            real = apply_real_seoul_data(conn)
            apply_news_sentiment(conn)
            n = generate_properties(conn)
            mode = "시연 샘플"
        conn.commit()
        print(f"[etl] 완료: {DB_PATH}")
        sale_src = "실데이터" if real.get("sale") else "FALLBACK"
        jeonse_src = "실데이터" if real.get("jeonse") else "FALLBACK"
        print(f"[etl] 모드={mode} · 자치구 {len(DISTRICTS)}개 "
              f"(매매={sale_src}, 전세={jeonse_src}), 금융상품 {len(PRODUCTS)}개, 매물 {n}건")
        dist = conn.execute(
            "SELECT risk_grade, COUNT(*) FROM v_property_latest_risk "
            "GROUP BY risk_grade").fetchall()
        print("[etl] 위험등급 분포:", dict(dist))
        prod = conn.execute(
            "SELECT recommended_product, COUNT(*) FROM v_property_latest_risk "
            "GROUP BY recommended_product").fetchall()
        print("[etl] 추천 KB상품 분포:", dict(prod))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
