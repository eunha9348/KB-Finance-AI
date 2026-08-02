"""KB국민은행 전세 관련 상품 마스터 + 위험도 기반 추천 로직.

금리·한도는 kbthink.com 등에 공개된 상품안내 기준 참고값이다. 실제 적용 조건은
신용도와 COFIX 변동에 따라 달라지므로 실서비스에서는 은행 API 조회로 바꿔야 한다.
KB 청년 맞춤형처럼 변동금리라 고정 수치가 없는 상품은 rate_min/max 를 None 으로 두고
rate_asof 에 설명만 넣었다.

reference_url 은 obank.kbstar.com 같은 내부뱅킹 딥링크를 쓰면 로그인 세션이 없을 때
빈 화면이 떠서, 세션 없이도 열리는 공식 안내 페이지 경로로 잡았다.
"""

from dataclasses import dataclass

# 청년전용 버팀목 보증금 한도(수도권 일반 기준). 세대 유형별로 다르지만 대표값 사용.
BEOTIMOK_DEPOSIT_LIMIT_MANWON = 30000

# KB스타 전세자금대출(HUG/HF)의 채권보전조치 포함 최대한도.
# 이걸 넘으면 SGI 연계 상품 말고는 커버가 안 된다.
HF_HUG_MAX_COVERAGE_MANWON = 44400

# 경고 등급에서 HUG 반환보증 결합형과 저비용 HF형을 가르는 선순위채권비율
WARNING_HUG_COMBO_THRESHOLD = 0.90

# 안전 등급 매물에 붙이는 우대금리(%p). 공시금리가 아니라 이 제안에서 신설하는 정책.
PLATFORM_SAFE_BONUS = -0.3

PRODUCTS = {
    "BEOTIMOK_YOUTH": dict(
        product_code="BEOTIMOK_YOUTH",
        name="청년전용 버팀목 전세자금대출",
        provider="주택도시기금 (KB국민은행 등 수탁은행 취급)",
        product_type="정책대출",
        guarantee_agency="주택도시보증공사(HUG) 협약",
        rate_min=2.0, rate_max=3.1,
        rate_asof="정부 고시금리(분기별 변동) 참고값",
        max_loan_manwon=20000,
        max_deposit_manwon=BEOTIMOK_DEPOSIT_LIMIT_MANWON,
        target_grade="안전·주의",
        description=("무주택 청년(만 19~34세) 대상 정부 정책 전세자금대출. 시중 은행 자체상품보다 "
                     "낮은 금리로, 보증금 3억원(수도권 일반 기준) 이하 물건에 우선 매칭된다."),
        reference_url="https://kbthink.com/loan-guide/beotimok-youth.html",
    ),
    "KB_YOUTH_JEONSE": dict(
        product_code="KB_YOUTH_JEONSE",
        name="KB 청년 맞춤형 전세자금대출",
        provider="KB국민은행",
        product_type="은행자체대출(청년특화)",
        guarantee_agency="한국주택금융공사(HF)",
        rate_min=None, rate_max=None,
        rate_asof="신규취급액기준 COFIX + 가산금리 (변동금리, 실시간 금리는 은행 확인 필요)",
        max_loan_manwon=20000,
        max_deposit_manwon=None,
        target_grade="안전·주의 (버팀목 한도 초과)",
        description=("만 19~34세 무주택 청년 대상 KB 자체 전세자금대출. 임차보증금의 90% 이내, "
                     "최대 2억원. 한국주택금융공사(HF) 보증료 우대, 중도상환수수료 없음. "
                     "정부 정책자금 한도를 초과하는 보증금 물건에 매칭된다."),
        reference_url="https://kbthink.com/loan-guide/kb-youth-jeonse.html",
    ),
    "KB_STAR_HUG": dict(
        product_code="KB_STAR_HUG",
        name="KB스타 전세자금대출 (HUG 전세금안심대출보증)",
        provider="KB국민은행",
        product_type="은행자체대출 + 보증부(HUG)",
        guarantee_agency="주택도시보증공사(HUG)",
        rate_min=3.94, rate_max=5.34,
        rate_asof="2025-05 공시 기준 참고값 (변동)",
        max_loan_manwon=22200,
        max_deposit_manwon=None,
        target_grade="경고",
        description=("전세보증금반환보증이 함께 결합되는 HUG 연계 전세자금대출. 임차보증금의 "
                     "최대 80% 이내(채권보전조치 시 한도 확대 가능). 선순위채권비율이 높아 "
                     "반환보증 가입이 필요한 매물에 우선 매칭된다."),
        reference_url="https://www.khug.or.kr/khmb/m/hg/gg/relax/relaxsub3.jsp",
    ),
    "KB_STAR_HF": dict(
        product_code="KB_STAR_HF",
        name="KB스타 전세자금대출 (HF 한국주택금융공사)",
        provider="KB국민은행",
        product_type="은행자체대출 + 보증부(HF)",
        guarantee_agency="한국주택금융공사(HF)",
        rate_min=3.70, rate_max=None,
        rate_asof="2024년 공시 기준 최저 연 3.70%부터 (신용도별 상단은 은행 확인 필요)",
        max_loan_manwon=22200,
        max_deposit_manwon=None,
        target_grade="경고 (선순위채권비율 상대적으로 낮은 구간)",
        description=("한국주택금융공사(HF) 전세자금보증을 담보로 하는 은행자체 전세자금대출. "
                     "임차보증금의 최대 80% 이내, 최고 2억 2,200만원(채권보전조치 시 "
                     "4억 4,400만원). HF 보증료는 임차보증금의 0.02~0.4%로 HUG 결합형보다 "
                     "저렴해, 선순위채권 부담이 상대적으로 낮은 경고등급 매물에 매칭된다."),
        reference_url="https://kbthink.com/life/daily/hf-hug-sgi.html",
    ),
    "KB_STAR_SGI": dict(
        product_code="KB_STAR_SGI",
        name="KB스타 전세자금대출 (SGI 서울보증)",
        provider="KB국민은행",
        product_type="은행자체대출 + 보증부(SGI)",
        guarantee_agency="서울보증보험(SGI)",
        rate_min=3.70, rate_max=None,
        rate_asof="2025-05 공시 기준 최저 연 3.70%부터 (신용도·한도구간별 변동)",
        max_loan_manwon=50000,
        max_deposit_manwon=None,
        target_grade="전체 등급 (HUG·HF 한도를 초과하는 고액 전세)",
        description=("서울보증보험(SGI) 임차자금보험을 담보로 하는 은행자체 전세자금대출. "
                     "최소 500만원~최대 5억원(신용평점·임차보증금의 80%·부부합산 1주택 시 "
                     "최대 3억원 중 적은 금액). 아파트 등 보증한도 제한이 상대적으로 적어, "
                     "HUG·HF 보증형 상품의 한도(4억 4,400만원)를 초과하는 고액 전세 물건에 "
                     "매칭된다."),
        reference_url="https://kbthink.com/life/daily/hf-hug-sgi.html",
    ),
    "HUG_ANSIM_MANDATORY": dict(
        product_code="HUG_ANSIM_MANDATORY",
        name="전세보증금반환보증 가입 필수 안내 (대출 매칭 보류)",
        provider="주택도시보증공사(HUG)",
        product_type="보증보험 선가입 필수",
        guarantee_agency="HUG",
        rate_min=None, rate_max=None,
        rate_asof="대출 상품이 아닌 보증보험 안내 단계 (금리 해당 없음)",
        max_loan_manwon=None,
        max_deposit_manwon=None,
        target_grade="위험",
        description=("선순위채권(근저당+보증금)이 매매시세를 위협하는 고위험 매물입니다. "
                     "전세보증금반환보증 가입 가능 여부를 먼저 확인하고, 가입이 불가하다면 "
                     "계약 자체를 재검토해야 합니다. 이 단계에서는 대출 상품을 매칭하지 않습니다."),
        reference_url="https://www.khug.or.kr/hug/web/ig/dr/igdr000001.jsp",
    ),
}


@dataclass
class ProductMatch:
    product_code: str
    proposal_rate_adjust: float
    match_reason: str


def match_product(risk_score, risk_grade, jeonse_ratio, senior_debt_ratio,
                  deposit_manwon):
    """등급 + 선순위채권비율 + 보증금 규모를 같이 보고 상품 하나를 고른다.

    등급만 보고 1:1로 매핑하면 같은 '경고' 안에서도 조건이 전혀 다른 매물에
    똑같은 상품이 붙어버려서, 연속값인 선순위채권비율로 한 번 더 갈랐다.
    """
    if risk_grade == "위험":
        return ProductMatch(
            "HUG_ANSIM_MANDATORY", 0.0,
            f"선순위채권비율 {senior_debt_ratio:.0%}로 매매시세를 위협 → "
            f"보증보험 가입 여부 우선 확인 필요 (대출 매칭 보류)")

    # 보증금이 HUG·HF 한도를 넘으면 등급과 상관없이 SGI 말고는 답이 없다.
    if deposit_manwon > HF_HUG_MAX_COVERAGE_MANWON:
        return ProductMatch(
            "KB_STAR_SGI", 0.0,
            f"보증금 {deposit_manwon:,}만원이 HUG·HF 보증형 상품의 한도 "
            f"{HF_HUG_MAX_COVERAGE_MANWON:,}만원을 초과 → 한도가 넉넉한 SGI 연계 상품으로 매칭")

    if risk_grade == "경고":
        if senior_debt_ratio >= WARNING_HUG_COMBO_THRESHOLD:
            return ProductMatch(
                "KB_STAR_HUG", 0.0,
                f"선순위채권비율 {senior_debt_ratio:.0%} (≥{WARNING_HUG_COMBO_THRESHOLD:.0%}) → "
                f"HUG 전세보증금반환보증이 결합된 상품으로 매칭")
        return ProductMatch(
            "KB_STAR_HF", 0.0,
            f"선순위채권비율 {senior_debt_ratio:.0%} (<{WARNING_HUG_COMBO_THRESHOLD:.0%}) → "
            f"반환보증 결합 없이도 안전마진이 있어, 보증료가 더 저렴한 HF 보증 상품으로 매칭")

    # 안전/주의 등급은 정책자금이 금리가 제일 싸니까 한도 안에 들면 그쪽 우선
    if deposit_manwon <= BEOTIMOK_DEPOSIT_LIMIT_MANWON:
        return ProductMatch(
            "BEOTIMOK_YOUTH", 0.0,
            f"보증금 {deposit_manwon:,}만원 ≤ 정책자금 한도 "
            f"{BEOTIMOK_DEPOSIT_LIMIT_MANWON:,}만원 → 정부 최저금리 상품 우선 매칭")

    bonus = PLATFORM_SAFE_BONUS if risk_grade == "안전" else 0.0
    reason = f"보증금 {deposit_manwon:,}만원이 정책자금 한도를 초과해 KB 자체 청년전세대출로 매칭"
    if bonus:
        reason += f" · 안전매물 특별우대(제안) {bonus:+.1f}%p 추가 적용"
    return ProductMatch("KB_YOUTH_JEONSE", bonus, reason)
