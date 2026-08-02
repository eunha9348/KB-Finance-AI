# 사용법

## 제일 빠른 방법

`frontend/standalone.html` 더블클릭. 데이터가 파일에 내장돼 있어서 서버도
인터넷도 필요 없습니다.

## 데이터부터 다시 만들기

```bash
python data/etl.py               # db/housing.db
python data/export_json.py       # → frontend/data.json
python data/build_standalone.py  # → frontend/standalone.html
```

셋 다 순서대로 돌려야 standalone.html 에 반영됩니다.

## 백엔드 띄우기

```bash
python backend/app.py    # http://localhost:8000
```

Flask 가 있으면 Flask 로, 없으면 표준 라이브러리 서버로 뜹니다.
이쪽은 Leaflet 실지도 + Chart.js 를 써서 CDN 이 필요합니다.

## 실제 매물 넣기

[rt.molit.go.kr](https://rt.molit.go.kr) → 아파트 → 전월세 → 지역·기간 선택 →
CSV 다운로드. 인증키 없이 받을 수 있습니다.

받은 파일을 `data/realprice_rent.csv` 로 저장하고 `python data/etl.py`.
파일명이 `realprice` 로 시작하면 다 읽으니까 `realprice_rent_gangnam.csv` 처럼
쪼개서 넣어도 됩니다. 매매 CSV(`realprice_trade.csv`)를 같이 넣으면 매매가
추정에 씁니다.

컬럼명이 조금 달라도 '시군구', '단지명', '보증금' 같은 토큰이 헤더에 들어있으면
잡아냅니다. 못 찾은 행은 그냥 건너뜁니다.

## API 키 연동

둘 중 하나만 있으면 됩니다.

**국토부 (권장)** — data.go.kr 에서 '국토교통부_아파트 매매 실거래가 자료',
'전월세 실거래가 자료' 활용신청(자동승인) → 마이페이지에서 일반 인증키(Encoding) 복사.

**서울 열린데이터광장** — data.seoul.go.kr 마이페이지에서 인증키 신청.
[OA-21275](https://data.seoul.go.kr/dataList/OA-21275/S/1/datasetView.do) 매매,
[OA-21276](https://data.seoul.go.kr/dataList/OA-21276/S/1/datasetView.do) 전월세.

```bash
cp .env.example .env    # 여기에 키 채워넣기
python data/etl.py
```

서울 쪽 매매 엔드포인트는 ERROR-500 이 자주 납니다. 재시도를 넣어놨지만 그래도
실패하면 그 지표만 폴백으로 남고 로그에 이유가 찍힙니다.

컬럼 매칭이 안 되면 `응답에서 컬럼을 하나도 매칭하지 못했습니다` 와 함께
샘플 응답 키가 출력됩니다. 그 키를 `data/seoul_api.py` 의 `FIELD_CANDIDATES` 에
추가하면 됩니다.

## 위험도 기준 바꾸기

`data/risk_engine.py` 상단 상수를 고칩니다.

- `W_FUND` / `W_CONTEXT` — 펀더멘털과 지역 컨텍스트 비중
- `GRADE_BANDS` — 등급 임계값
- `BUILDING_TYPE_RISK` — 주거유형별 가산

고친 뒤 `python data/validate_risk.py` 를 돌리면 검증 문서가 새 값으로 다시
써집니다. 단조성이 깨지거나 시나리오가 FAIL 로 뜨면 가중치가 이상해진 겁니다.

## 상품 바꾸기

`data/kb_products.py` 의 `PRODUCTS` 와 `match_product()`. 고치고 나서 `etl.py`
부터 다시 돌려야 매칭 결과가 갱신됩니다.

```bash
python data/risk_engine.py    # 샘플 3건으로 매칭 확인
```

## DB 들여다보기

```bash
sqlite3 db/housing.db "SELECT risk_grade, recommended_product, COUNT(*) \
  FROM v_property_latest_risk GROUP BY 1,2"
```

`v_property_latest_risk` 뷰가 매물 + 최신 진단 결과를 조인해 둔 거라 이것만 보면
됩니다.
