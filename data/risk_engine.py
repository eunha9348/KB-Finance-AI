"""전세 매물 위험도 산출 엔진.

위험도 = 100 * (0.78 * f + 0.22 * c)
  f : 물건 자체의 금융 펀더멘털 (선순위채권비율 위주)
  c : 지역 컨텍스트 (전세가 변동성 + 뉴스 감성)

가중치를 f 쪽에 크게 준 이유는, 뉴스 몇 건 때문에 멀쩡한 매물이 '위험'으로
뒤집히면 안 되기 때문이다. 감성은 등급 경계선을 조금 밀어주는 정도만 한다.
가중치·임계값 검증은 validate_risk.py 참고.
"""

from dataclasses import dataclass, field

W_FUND = 0.78
W_CONTEXT = 0.22

# 컨텍스트 내부 배분
C_VOL = 0.45
C_SENTIMENT = 0.55
VOL_CV_CAP = 0.60          # 변동계수 0.6 이상은 전부 최고위험 취급

JEONSE_PREMIUM_MAX = 0.12  # 전세가율 0.8~1.0 구간 가산
ILLEGAL_PREMIUM = 0.12
BUILDING_TYPE_RISK = {
    "아파트": 0.00,
    "오피스텔": 0.04,
    "도시형생활주택": 0.05,
    "다세대": 0.06,
    "빌라": 0.07,
}
AGE_PREMIUM_MAX = 0.05
AGE_FULL_YEARS = 25
REFERENCE_YEAR = 2026      # 노후도 기준연도. 재현성 때문에 고정값 사용

GRADE_BANDS = [
    (0, 30, "안전"),
    (30, 55, "주의"),
    (55, 80, "경고"),
    (80, 101, "위험"),
]


@dataclass
class RiskInput:
    sale_price: int              # 추정 매매 시세 (만원)
    deposit: int                 # 전세 보증금 (만원)
    mortgage_amount: int         # 근저당 설정액 (만원)
    is_illegal: bool = False
    building_type: str = "아파트"
    build_year: int = None
    district_jeonse_cv: float = 0.0
    district_sentiment: float = 0.0
    city_sentiment_baseline: float = 0.0


@dataclass
class RiskResult:
    jeonse_ratio: float
    senior_debt_ratio: float
    risk_score: int
    risk_grade: str
    fundamental_score: float = 0.0
    context_score: float = 0.0
    components: dict = field(default_factory=dict)


def _grade_of(score):
    for low, high, grade in GRADE_BANDS:
        if low <= score < high:
            return grade
    return "위험"


def _fundamental(inp, jeonse_ratio, senior_debt_ratio):
    # 선순위채권비율이 기본값을 잡는다. 50% 아래면 사실상 안전이고,
    # 100%를 넘으면 경매로 넘어갔을 때 보증금을 다 못 받는다.
    if senior_debt_ratio <= 0.5:
        base = 0.0
    elif senior_debt_ratio <= 1.0:
        base = (senior_debt_ratio - 0.5) / 0.5 * 0.85
    else:
        base = 0.85 + min((senior_debt_ratio - 1.0) / 0.3, 1.0) * 0.15

    jeonse_prem = 0.0
    if jeonse_ratio > 0.8:
        jeonse_prem = min((jeonse_ratio - 0.8) / 0.2, 1.0) * JEONSE_PREMIUM_MAX

    illegal_prem = ILLEGAL_PREMIUM if inp.is_illegal else 0.0
    type_prem = BUILDING_TYPE_RISK.get(inp.building_type, 0.03)

    age_prem = 0.0
    if inp.build_year:
        age = max(REFERENCE_YEAR - int(inp.build_year), 0)
        age_prem = min(age / AGE_FULL_YEARS, 1.0) * AGE_PREMIUM_MAX

    f = min(base + jeonse_prem + illegal_prem + type_prem + age_prem, 1.0)
    return f, {
        "senior_debt_base": round(base, 4),
        "jeonse_premium": round(jeonse_prem, 4),
        "illegal_premium": illegal_prem,
        "building_type_premium": type_prem,
        "age_premium": round(age_prem, 4),
    }


def _context(inp):
    # 감성 점수를 그대로 더하면 서울 전체가 통째로 올라가서 변별력이 없어진다.
    # 기준선을 넘는 만큼만 반영해야 피해다발 자치구만 튄다.
    vol = min(max(inp.district_jeonse_cv, 0.0) / VOL_CV_CAP, 1.0)
    base = min(max(inp.city_sentiment_baseline, 0.0), 0.999)
    sent = min(max(inp.district_sentiment, 0.0), 1.0)
    excess = max(sent - base, 0.0) / (1.0 - base) if base < 1.0 else 0.0
    excess = min(excess, 1.0)
    c = min(C_VOL * vol + C_SENTIMENT * excess, 1.0)
    return c, {"volatility_norm": round(vol, 4),
               "sentiment": round(sent, 4),
               "sentiment_excess": round(excess, 4)}


def assess(inp):
    """위험도 0~100 산출. 클수록 위험."""
    sale = max(inp.sale_price, 1)
    jeonse_ratio = round(inp.deposit / sale, 4)
    senior_debt_ratio = round((inp.mortgage_amount + inp.deposit) / sale, 4)

    f, f_parts = _fundamental(inp, jeonse_ratio, senior_debt_ratio)
    c, c_parts = _context(inp)

    score = int(round(min(100 * (W_FUND * f + W_CONTEXT * c), 100)))
    return RiskResult(
        jeonse_ratio=jeonse_ratio,
        senior_debt_ratio=senior_debt_ratio,
        risk_score=score,
        risk_grade=_grade_of(score),
        fundamental_score=round(f, 4),
        context_score=round(c, 4),
        components={**f_parts, **c_parts,
                    "w_fund": W_FUND, "w_context": W_CONTEXT},
    )


if __name__ == "__main__":
    from kb_products import match_product

    BASE = 0.47
    samples = [
        ("안전 아파트/저채권", RiskInput(30000, 12000, 3000, building_type="아파트",
                                    build_year=2015, district_jeonse_cv=0.2,
                                    district_sentiment=0.47, city_sentiment_baseline=BASE)),
        ("경고 빌라/높은채권/관악", RiskInput(25000, 20000, 4000, building_type="빌라",
                                     build_year=2005, district_jeonse_cv=0.5,
                                     district_sentiment=0.79, city_sentiment_baseline=BASE)),
        ("위험 깡통전세", RiskInput(20000, 19000, 8000, building_type="다세대",
                               build_year=2000, is_illegal=True,
                               district_jeonse_cv=0.55, district_sentiment=0.79,
                               city_sentiment_baseline=BASE)),
    ]
    for label, s in samples:
        r = assess(s)
        m = match_product(r.risk_score, r.risk_grade, r.jeonse_ratio,
                          r.senior_debt_ratio, s.deposit)
        print(f"[{label}] 전세가율 {r.jeonse_ratio:.0%} 선순위 {r.senior_debt_ratio:.0%} "
              f"| f={r.fundamental_score} c={r.context_score} → {r.risk_score}점 "
              f"[{r.risk_grade}] → {m.product_code}")
