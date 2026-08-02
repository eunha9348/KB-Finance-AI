"""프로토타입 백엔드.

  GET /api/properties        매물 + 최신 위험도
  GET /api/properties/<id>   매물 상세 + 추천 상품
  GET /api/districts         자치구별 평균
  GET /api/stats             요약 통계
  GET /                      frontend/index.html

Flask 가 깔려있으면 Flask 로, 없으면 http.server 로 뜬다. 심사할 때 pip install
없이 바로 돌려볼 수 있어야 해서 폴백을 넣었다.

    python backend/app.py   →  http://localhost:8000
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "housing.db"
FRONTEND = ROOT / "frontend"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_properties():
    with _conn() as c:
        rows = c.execute("SELECT * FROM v_property_latest_risk").fetchall()
    return [dict(r) for r in rows]


def get_property(pid):
    with _conn() as c:
        row = c.execute("SELECT * FROM v_property_latest_risk WHERE property_id=?",
                        (pid,)).fetchone()
        if not row:
            return None
        prop = dict(row)
        prod = c.execute("SELECT * FROM finance_products WHERE product_code=?",
                         (prop["recommended_product"],)).fetchone()
        prop["product_detail"] = dict(prod) if prod else None
    return prop


def get_districts():
    with _conn() as c:
        rows = c.execute("""
            SELECT d.district_code, d.name, d.lat, d.lng,
                   d.avg_sale_price, d.avg_jeonse,
                   d.sale_source, d.jeonse_source, d.jeonse_cv,
                   d.news_sentiment, d.sentiment_note,
                   COUNT(v.property_id) AS n,
                   ROUND(AVG(v.risk_score),1) AS avg_risk,
                   ROUND(AVG(v.jeonse_ratio),3) AS avg_jeonse_ratio
            FROM districts d
            LEFT JOIN v_property_latest_risk v
              ON v.district_name = d.name
            GROUP BY d.district_code
            ORDER BY avg_risk DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_stats():
    with _conn() as c:
        grade = dict(c.execute(
            "SELECT risk_grade, COUNT(*) FROM v_property_latest_risk "
            "GROUP BY risk_grade").fetchall())
        total = c.execute("SELECT COUNT(*) FROM v_property_latest_risk").fetchone()[0]
        avg_risk = c.execute(
            "SELECT ROUND(AVG(risk_score),1) FROM v_property_latest_risk").fetchone()[0]
        # 지표별로 몇 개 구가 실데이터인지 (프론트 상단 출처 배지에 쓰인다)
        sale_real = c.execute("SELECT COUNT(*) FROM districts WHERE sale_source!='fallback'").fetchone()[0]
        jeonse_real = c.execute("SELECT COUNT(*) FROM districts WHERE jeonse_source!='fallback'").fetchone()[0]
        n_districts = c.execute("SELECT COUNT(*) FROM districts").fetchone()[0]
        # 근거 없는 구는 기준선을 그대로 물려받으니 최소값이 곧 기준선이다
        city_sentiment = c.execute(
            "SELECT ROUND(MIN(news_sentiment),4) FROM districts").fetchone()[0]
        avg_context = c.execute(
            "SELECT ROUND(AVG(context_score),3) FROM v_property_latest_risk").fetchone()[0]
        avg_fund = c.execute(
            "SELECT ROUND(AVG(fundamental_score),3) FROM v_property_latest_risk").fetchone()[0]
        # 적재된 거래의 날짜 범위. 데이터가 언제 것인지 UI 에 띄우려고
        tx = c.execute(
            "SELECT COUNT(*), MIN(deal_date), MAX(deal_date) FROM transactions").fetchone()
        tx_count, tx_from, tx_to = tx[0], tx[1], tx[2]
    return {
        "total": total, "avg_risk": avg_risk, "grade_distribution": grade,
        "provenance": {
            "n_districts": n_districts,
            "sale_price_real": sale_real, "jeonse_price_real": jeonse_real,
            "city_sentiment_baseline": city_sentiment,
            "tx_count": tx_count, "tx_from": tx_from, "tx_to": tx_to,
        },
        "avg_context_score": avg_context, "avg_fundamental_score": avg_fund,
    }


def make_flask_app():
    from flask import Flask, jsonify, send_from_directory
    app = Flask(__name__, static_folder=None)

    @app.route("/api/properties")
    def _props():
        return jsonify(get_properties())

    @app.route("/api/properties/<int:pid>")
    def _prop(pid):
        p = get_property(pid)
        return (jsonify(p), 200) if p else (jsonify({"error": "not found"}), 404)

    @app.route("/api/districts")
    def _districts():
        return jsonify(get_districts())

    @app.route("/api/stats")
    def _stats():
        return jsonify(get_stats())

    @app.route("/")
    def _index():
        return send_from_directory(FRONTEND, "index.html")

    return app


def run_stdlib(port=8000):
    """Flask 없을 때 쓰는 최소 서버."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    routes = {
        "/api/properties": get_properties,
        "/api/districts": get_districts,
        "/api/stats": get_stats,
    }

    class H(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in routes:
                return self._send(routes[path]())
            if path.startswith("/api/properties/"):
                pid = int(path.rsplit("/", 1)[1])
                p = get_property(pid)
                return self._send(p or {"error": "not found"}, 200 if p else 404)
            if path in ("/", "/index.html"):
                html = (FRONTEND / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                return self.wfile.write(html)
            self._send({"error": "not found"}, 404)

        def log_message(self, *a):
            pass    # 요청마다 로그 찍히는 게 시끄러워서 껐다

    print(f"[backend/stdlib] http://localhost:{port}")
    HTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    try:
        make_flask_app().run(host="0.0.0.0", port=8000, debug=False)
    except ImportError:
        print("[backend] Flask 미설치 → 표준 라이브러리 서버로 실행")
        run_stdlib(8000)
