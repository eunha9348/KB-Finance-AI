"""DB → frontend/data.json. 지도랑 차트가 이 파일 하나만 보면 된다."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "data"))
import app  # noqa: E402
import news_sentiment  # noqa: E402

def get_products():
    with app._conn() as c:
        rows = c.execute("SELECT * FROM finance_products").fetchall()
    return {r["product_code"]: dict(r) for r in rows}


def get_news():
    """감성 분석 결과 + 코퍼스 통째로. 인스펙터에서 기사 링크를 바로 열 수 있게."""
    data = news_sentiment.load()
    analysis = news_sentiment.analyze()
    return {
        "city_index": analysis["city_index"],
        "risk_mass": analysis["risk_mass"],
        "n_articles": analysis["n_articles"],
        "terms": [t for t in analysis["terms"] if t["df"]],
        "corpus": data["corpus"],
        "district_signals": data.get("district_signals", {}),
        "lexicon": data["lexicon_2gram"],
        "collected_on": data["_meta"].get("collected_on"),
    }


out = {
    "properties": app.get_properties(),
    "districts": app.get_districts(),
    "stats": app.get_stats(),
    "products": get_products(),
    "news": get_news(),
}
dest = ROOT / "frontend" / "data.json"
dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"[export] {dest}  (매물 {len(out['properties'])}건)")
