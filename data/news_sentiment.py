"""전세시장 뉴스 감성분석 (2-gram 위험 렉시콘).

전세가율이나 선순위채권비율만으로는 "이 동네가 요즘 시끄럽다"는 신호가 안 잡힌다.
특정 자치구에 전세사기 보도가 몰리면 그 지역 계약 위험이 실제로 올라가므로,
기사에서 위험 2-gram 을 세어 지역 지수로 만든다.

  risk_mass = Σ(가중치 × 문서빈도)
  city_index = 1 - exp(-risk_mass / SCALE)
  자치구 점수 = city_index + (1-city_index) * level * DISTRICT_GAIN

렉시콘과 코퍼스는 news_sentiment.json 에 있고, 기사마다 URL 을 같이 적어뒀다.
자치구 실명이 나온 근거가 없으면 서울 기준선을 그대로 물려받는다.
"""

import json
import math
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "news_sentiment.json"

# 코퍼스를 '전세사기' 키워드로 모았기 때문에 위험 2-gram 이 거의 모든 기사에 들어있다.
# SCALE 을 작게 잡으면 city_index 가 0.9 넘게 나와서 전 자치구가 위험으로 뜬다.
# 기준선이 0.5 근처에 오도록 키워서, 감성이 펀더멘털을 덮어쓰지 않게 했다.
SCALE = 48.0
EASING_STEP = 0.04     # 완화 신호 기사 1건당 감산 폭
DISTRICT_GAIN = 0.6    # 자치구 근거가 기준선 위로 끌어올릴 수 있는 최대 비중

_CACHE = None


def load():
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return _CACHE


def _despace(s: str) -> str:
    return "".join((s or "").split())


def _article_text(a: dict) -> str:
    return f"{a.get('title', '')} {a.get('facts', '')}"


def _bigram_key(term: str) -> str:
    # '전세 사기' → '전세사기'. 기사마다 띄어쓰기가 제각각이라 공백을 없애고 비교한다.
    return _despace(term)


def analyze():
    """코퍼스 전체 2-gram 매칭 + 서울 위험지수."""
    data = load()
    lex = data["lexicon_2gram"]
    corpus = data["corpus"]

    # 기사별 despaced 텍스트
    docs = [(_despace(_article_text(a)), a) for a in corpus]

    term_stats = []
    risk_mass = 0.0
    for entry in lex:
        term = entry["term"]
        key = _bigram_key(term)
        hits = [a for despaced, a in docs if key in despaced]
        df = len(hits)
        term_stats.append({
            "term": term, "weight": entry["weight"],
            "polarity": entry["polarity"], "df": df,
            "articles": [h["url"] for h in hits],
        })
        if entry["polarity"] == "risk":
            risk_mass += entry["weight"] * df

    n_easing = sum(1 for a in corpus if a.get("polarity_hint") == "easing")

    city_index = 1.0 - math.exp(-risk_mass / SCALE)
    city_index = max(0.0, city_index - EASING_STEP * n_easing)
    city_index = round(min(city_index, 1.0), 4)

    return {
        "city_index": city_index,
        "risk_mass": round(risk_mass, 2),
        "n_articles": len(corpus),
        "n_easing": n_easing,
        "terms": sorted(term_stats, key=lambda t: -t["weight"] * t["df"]),
    }


def district_sentiment(name: str):
    """자치구명 → 감성 점수(0~1)와 근거. 근거 없으면 서울 기준선 그대로."""
    data = load()
    base = analyze()["city_index"]
    sig = data.get("district_signals", {}).get(name)
    if not sig:
        return {"name": name, "score": base, "base_city": base,
                "signal_level": 0.0, "evidence": [], "inherited": True,
                "note": "실명 근거 없음 → 서울 기준선 상속"}
    level = float(sig.get("level", 0.0))
    score = base + (1.0 - base) * level * DISTRICT_GAIN
    return {"name": name, "score": round(min(score, 1.0), 4), "base_city": base,
            "signal_level": level, "evidence": sig.get("evidence", []),
            "inherited": False, "note": sig.get("note", "")}


def all_district_scores(names):
    return {n: district_sentiment(n) for n in names}


if __name__ == "__main__":
    a = analyze()
    print(f"[news] 기사 {a['n_articles']}건, risk_mass={a['risk_mass']}, "
          f"완화신호 {a['n_easing']}건 → 서울 city_index={a['city_index']}")
    print("[news] 상위 2-gram 위험어(가중×빈도):")
    for t in a["terms"][:8]:
        if t["df"]:
            print(f"   - {t['term']:8s} w{t['weight']} × df{t['df']}  {t['articles']}")
    print("[news] 자치구 감성 예시:")
    for d in ["관악구", "강서구", "구로구", "노원구", "성동구"]:
        s = district_sentiment(d)
        tag = "상속" if s["inherited"] else f"근거{len(s['evidence'])}건"
        print(f"   - {d}: score={s['score']} ({tag}) {s['note']}")
