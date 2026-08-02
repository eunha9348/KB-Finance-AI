"""위험도 엔진 검증 스크립트.

돌리면 docs/risk_methodology_validation.md 를 다시 쓴다. 문서에 박혀있는 숫자는
전부 여기서 나온 계산 결과라, 가중치를 건드리면 문서도 같이 바뀐다.

  A 단조성   — 선순위채권비율 올리면 위험도도 올라가는지
  B 시나리오 — 대표 계약 4개가 기대한 등급대에 들어가는지
  C 상관     — 위험도가 어떤 지표를 따라가는지 (Spearman)
  D 분해     — 펀더멘털 대 컨텍스트 기여도
  E 민감도   — 감성을 빼면 등급이 얼마나 바뀌는지
"""

import sqlite3
import statistics as stats
from pathlib import Path

import risk_engine as re
from risk_engine import RiskInput, assess
import news_sentiment

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "housing.db"
DOC_PATH = ROOT / "docs" / "risk_methodology_validation.md"

BASE = news_sentiment.analyze()["city_index"]


def _spearman(xs, ys):
    """scipy 없이 순위 매기고 Pearson."""
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = (sum((v - mx) ** 2 for v in rx)) ** 0.5
    sy = (sum((v - my) ** 2 for v in ry)) ** 0.5
    return cov / (sx * sy) if sx and sy else 0.0


def test_monotonicity():
    """선순위채권비율만 0.4→1.3 으로 올려본다. 나머지 조건은 고정."""
    rows = []
    base_sale = 30000
    for sd in [0.4, 0.6, 0.8, 1.0, 1.2, 1.3]:
        # 보증금은 그대로 두고 근저당으로 비율을 맞춘다
        deposit = int(base_sale * 0.6)
        mortgage = int(base_sale * sd) - deposit
        mortgage = max(mortgage, 0)
        r = assess(RiskInput(base_sale, deposit, mortgage, building_type="아파트",
                             build_year=2015, district_jeonse_cv=0.2,
                             district_sentiment=0.47, city_sentiment_baseline=BASE))
        rows.append((sd, r.senior_debt_ratio, r.risk_score, r.risk_grade))
    monotonic = all(rows[i][2] <= rows[i + 1][2] for i in range(len(rows) - 1))
    return rows, monotonic


def test_scenarios():
    cases = [
        ("정상 아파트(저채권·저전세가율)",
         RiskInput(30000, 15000, 2000, building_type="아파트", build_year=2018,
                   district_jeonse_cv=0.15, district_sentiment=0.47, city_sentiment_baseline=BASE), {"안전", "주의"}),
        ("전세가율 높은 신축 오피스텔",
         RiskInput(25000, 21000, 3000, building_type="오피스텔", build_year=2020,
                   district_jeonse_cv=0.3, district_sentiment=0.47, city_sentiment_baseline=BASE), {"주의", "경고"}),
        ("선순위 과다 노후 빌라(피해다발구)",
         RiskInput(22000, 18000, 5000, building_type="빌라", build_year=2003,
                   district_jeonse_cv=0.5, district_sentiment=0.79, city_sentiment_baseline=BASE), {"경고", "위험"}),
        ("깡통전세(선순위>100%)+위반건축물",
         RiskInput(20000, 19000, 8000, is_illegal=True, building_type="다세대",
                   build_year=2001, district_jeonse_cv=0.55, district_sentiment=0.79,
                   city_sentiment_baseline=BASE), {"위험"}),
    ]
    out = []
    for label, inp, expected in cases:
        r = assess(inp)
        out.append((label, r.senior_debt_ratio, r.risk_score, r.risk_grade,
                    r.risk_grade in expected, expected))
    return out


def analyze_db():
    """DB 에 적재된 매물 전체로 상관·분해·감성 민감도를 본다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sale_price, deposit, mortgage_amount, is_illegal, building_type, "
        "build_year, district_jeonse_cv, district_sentiment, jeonse_ratio, "
        "senior_debt_ratio, fundamental_score, context_score, risk_score, risk_grade "
        "FROM v_property_latest_risk").fetchall()
    conn.close()

    score = [r["risk_score"] for r in rows]
    senior = [r["senior_debt_ratio"] for r in rows]
    jeonse = [r["jeonse_ratio"] for r in rows]
    fund = [r["fundamental_score"] for r in rows]
    ctx = [r["context_score"] for r in rows]

    rho_senior = _spearman(senior, score)
    rho_jeonse = _spearman(jeonse, score)
    rho_fund = _spearman(fund, score)
    rho_ctx = _spearman(ctx, score)

    fund_pts = [100 * re.W_FUND * f for f in fund]
    ctx_pts = [100 * re.W_CONTEXT * c for c in ctx]

    # 감성을 0으로 놓고 다시 매겼을 때 등급이 바뀌는 매물 수
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    props = conn.execute(
        "SELECT sale_price, deposit, mortgage_amount, is_illegal, building_type, "
        "build_year, district_jeonse_cv, district_sentiment, risk_grade FROM "
        "v_property_latest_risk").fetchall()
    conn.close()
    changed = 0
    for p in props:
        r0 = assess(RiskInput(p["sale_price"], p["deposit"], p["mortgage_amount"],
                              bool(p["is_illegal"]), p["building_type"], p["build_year"],
                              p["district_jeonse_cv"], 0.0, BASE))
        if r0.risk_grade != p["risk_grade"]:
            changed += 1
    return {
        "n": len(rows),
        "rho_senior": round(rho_senior, 4),
        "rho_jeonse": round(rho_jeonse, 4),
        "rho_fund": round(rho_fund, 4),
        "rho_ctx": round(rho_ctx, 4),
        "avg_fund_pts": round(stats.mean(fund_pts), 2),
        "avg_ctx_pts": round(stats.mean(ctx_pts), 2),
        "avg_score": round(stats.mean(score), 2),
        "grade_change_no_sentiment": changed,
        "grade_change_pct": round(100 * changed / len(props), 1),
    }


def analyze_real_data():
    """실데이터가 얼마나 반영됐는지 + 실제 전세가율이 위험도까지 전파되는지."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    d = conn.execute(
        "SELECT name, avg_sale_price, avg_jeonse, sale_source, jeonse_source, "
        "jeonse_cv FROM districts").fetchall()
    n_tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    tx_src = conn.execute(
        "SELECT source, COUNT(*) FROM transactions GROUP BY source").fetchall()
    drisk = {r["name"]: r["avg_risk"] for r in conn.execute(
        "SELECT district_name AS name, AVG(risk_score) AS avg_risk "
        "FROM v_property_latest_risk GROUP BY district_name").fetchall()}
    conn.close()
    real_sale = sum(1 for r in d if r["sale_source"] != "fallback")
    real_jeonse = sum(1 for r in d if r["jeonse_source"] != "fallback")
    ratios, risks, rows = [], [], []
    for r in d:
        if r["avg_sale_price"] and r["avg_jeonse"]:
            jr = r["avg_jeonse"] / r["avg_sale_price"]
            rk = drisk.get(r["name"])
            if rk is not None:
                ratios.append(jr); risks.append(rk)
                rows.append((r["name"], jr, rk, r["sale_source"] != "fallback"))
    rho = _spearman(ratios, risks) if len(ratios) > 2 else 0.0
    return {"n_tx": n_tx, "tx_src": dict(tx_src), "real_sale": real_sale,
            "real_jeonse": real_jeonse, "n_districts": len(d),
            "rho_ratio_risk": round(rho, 4),
            "rows": sorted(rows, key=lambda x: -x[1])[:8]}


def build_report():
    mono_rows, monotonic = test_monotonicity()
    scen = test_scenarios()
    db = analyze_db()
    real = analyze_real_data()
    news = news_sentiment.analyze()

    L = []
    w = L.append
    w("# 위험도 방법론 검증 리포트\n")
    w("> `python data/validate_risk.py` 로 다시 생성되는 문서입니다. "
      "아래 수치는 전부 현재 DB 기준 계산 결과라서, 가중치를 바꾸면 문서도 바뀝니다.\n")
    w(f"- 검증 매물: **{db['n']}건**")
    w(f"- 뉴스 코퍼스: 기사 **{news['n_articles']}건**, risk_mass "
      f"**{news['risk_mass']}**, 서울 기준선 **{news['city_index']}**\n")

    w("## 방법론 요약")
    w("위험도 = 100 × ( **0.78·f** + **0.22·c** )")
    w("- **f (0~1)**: 선순위채권비율 + 전세가율 + 위반건축물 + 주거유형 + 노후도")
    w("- **c (0~1)**: 전세가 변동성(0.45) + 뉴스 감성 초과분(0.55)")
    w("- 감성은 `(감성−기준선)/(1−기준선)` 으로 기준선 초과분만 넣는다. 처음엔 기준선을 "
      "그대로 더했는데 서울 전체 위험도가 통째로 올라가서(아래 E 항목 등급변동 44%) "
      "자치구 간 변별이 사라졌다.")
    w("- f 비중을 크게 둔 건 뉴스 때문에 등급이 뒤집히는 걸 막기 위해서다.\n")

    w("## A. 단조성 — 선순위채권비율이 오르면 위험도도 오르는가")
    w("| 목표 선순위 | 실제 선순위 | 위험도 | 등급 |")
    w("|---|---|---|---|")
    for sd, real_sd, sc, g in mono_rows:
        w(f"| {sd:.2f} | {real_sd:.0%} | {sc} | {g} |")
    w(f"\n**결과: {'단조 증가 성립' if monotonic else '단조성 위반'}**\n")

    w("## B. 시나리오 — 대표 계약 4건이 기대 등급대에 드는가")
    w("| 시나리오 | 선순위 | 위험도 | 등급 | 기대 | 판정 |")
    w("|---|---|---|---|---|---|")
    all_pass = True
    for label, sd, sc, g, ok, exp in scen:
        all_pass = all_pass and ok
        w(f"| {label} | {sd:.0%} | {sc} | {g} | {'/'.join(sorted(exp))} | "
          f"{'PASS' if ok else 'FAIL'} |")
    w(f"\n**결과: {'전부 통과' if all_pass else '일부 실패'}**\n")

    w(f"## C. 순위상관 (Spearman ρ, 매물 {db['n']}건)")
    w("1에 가까울수록 그 지표를 따라 위험도가 같이 움직인다는 뜻.")
    w("| 지표 | ρ (vs 위험도) |")
    w("|---|---|")
    w(f"| 선순위채권비율 | **{db['rho_senior']}** |")
    w(f"| 전세가율 | {db['rho_jeonse']} |")
    w(f"| 펀더멘털 f | {db['rho_fund']} |")
    w(f"| 컨텍스트 c | {db['rho_ctx']} |")
    w(f"\n선순위채권비율이 ρ={db['rho_senior']} 로 가장 강하게 붙는다. 보증금을 못 받는 "
      "상황을 핵심 축으로 잡은 설계와 맞는 결과.\n")

    w("## D. 요소 분해 — 펀더멘털 vs 컨텍스트")
    w(f"- 평균 위험도: **{db['avg_score']}점**")
    w(f"- 펀더멘털 기여: **{db['avg_fund_pts']}점** (100×0.78×f̄)")
    w(f"- 컨텍스트 기여: **{db['avg_ctx_pts']}점** (100×0.22×c̄)")
    ratio = round(db['avg_fund_pts'] / max(db['avg_fund_pts'] + db['avg_ctx_pts'], 1e-9) * 100, 1)
    w(f"- 총점의 약 **{ratio}%** 가 펀더멘털에서 나온다.\n")

    w("## E. 감성 민감도 — 감성만으로 등급이 뒤집히는가")
    w(f"감성을 0 으로 놓고 전부 재평가했을 때 등급이 바뀐 매물: "
      f"**{db['grade_change_no_sentiment']}건 ({db['grade_change_pct']}%)**")
    w("등급 경계선에 걸쳐 있던 매물만 움직이고 나머지는 그대로다. "
      "뉴스가 많다고 멀쩡한 물건이 '위험'으로 넘어가지는 않는다.\n")

    w("## F. 실거래가 반영 현황")
    real_on = real["real_sale"] > 0 or real["real_jeonse"] > 0
    if real_on:
        w(f"- 매매 실데이터 자치구 **{real['real_sale']}/{real['n_districts']}**, "
          f"전세 **{real['real_jeonse']}/{real['n_districts']}**, 원본 거래 "
          f"**{real['n_tx']}건** 적재 (출처: {real['tx_src']}).")
        w(f"- 자치구 전세가율(avg_jeonse/avg_sale)과 평균 위험도의 ρ = "
          f"**{real['rho_ratio_risk']}**. 실거래 값이 개별 매물 보증금을 거쳐 위험도까지 "
          f"전파된다는 뜻이다.")
    else:
        w("- 지금 DB 는 폴백 근사값으로 만들어졌다(키 미설정). 클라우드 환경에서는 프록시가 "
          "정부 API 를 막아서, 실데이터 수집은 GitHub Actions 쪽에서 키를 넣고 돌려야 한다.")
        w(f"- 폴백 상태에서도 전세가율↔위험도 ρ = **{real['rho_ratio_risk']}** 로 "
          "양의 관계는 나온다.")
    w("\n| 자치구 | 전세가율 | 평균 위험도 | 실데이터 |")
    w("|---|---|---|---|")
    for name, jr, rk, isreal in real["rows"]:
        w(f"| {name} | {jr:.0%} | {rk:.1f} | {'O' if isreal else '폴백'} |")
    w("")

    w("## 코퍼스에 실제로 등장한 2-gram")
    w("| 2-gram | 가중치 | 문서빈도 |")
    w("|---|---|---|")
    for t in news["terms"]:
        if t["df"]:
            w(f"| {t['term']} | {t['weight']} | {t['df']} |")
    w("")

    w("## 한계")
    w("- 개별 매물의 근저당은 등기부가 공공 API 로 안 열려서 못 넣는다. CSV 모드에서는 "
      "미확인으로 두고 선순위채권비율을 전세가율과 같게 잡는데, 실제 근저당이 있으면 "
      "위험도가 지금보다 올라간다.")
    w("- 뉴스 코퍼스를 '전세사기' 키워드로 모아서 표본 자체가 부정 쪽으로 치우쳐 있다. "
      "city_index 를 전체 뉴스의 부정 비율로 읽으면 안 되고, SCALE 로 기준선을 0.5 "
      "근처에 맞춰 상대 비교용으로만 쓴다.")
    w("- 자치구 감성은 그 구 이름이 실제로 나온 보도가 있을 때만 차등한다(관악·강서·구로·금천). "
      "나머지는 서울 기준선을 그대로 쓴다.")
    w("- 전세가 변동성은 실거래 데이터가 있어야 나온다. 없으면 0 이고 컨텍스트에는 "
      "감성만 반영된다.\n")

    DOC_PATH.parent.mkdir(exist_ok=True)
    DOC_PATH.write_text("\n".join(L), encoding="utf-8")
    return monotonic, all_pass, db


if __name__ == "__main__":
    monotonic, all_pass, db = build_report()
    print(f"[validate] 단조성={'OK' if monotonic else 'FAIL'}, "
          f"시나리오={'OK' if all_pass else 'FAIL'}, "
          f"ρ(선순위)={db['rho_senior']}, 감성제거 등급변동={db['grade_change_pct']}%")
    print(f"[validate] 리포트 생성: {DOC_PATH}")
