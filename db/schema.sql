-- 전세 안전진단 프로토타입 스키마 (SQLite)
--
-- 모든 원천 데이터에 source 와 수집일시를 남긴다. 은행 심사에 쓰려면 이 값이
-- 어디서 왔는지 추적이 돼야 하고, 크롤링 데이터는 아예 못 들어오게 CHECK 로 막았다.
-- 위험도는 결과만 저장하지 않고 입력값도 같이 넣어서 나중에 재계산이 가능하게 했다.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS districts (
    district_code   TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    lat             REAL NOT NULL,
    lng             REAL NOT NULL,
    avg_sale_price  INTEGER,                   -- 평균 매매가 (만원)
    avg_jeonse      INTEGER,                   -- 평균 전세가 (만원)
    -- 매매만 성공하고 전세는 실패하는 경우가 있어서 출처를 따로 기록
    sale_source     TEXT DEFAULT 'fallback',
    jeonse_source   TEXT DEFAULT 'fallback',
    jeonse_cv       REAL DEFAULT 0,            -- 전세가 변동계수
    news_sentiment  REAL DEFAULT 0,            -- 0~1
    sentiment_note  TEXT
);

-- 실거래가 원본. raw_ref 에 법정동명/단지명을 남겨두면 나중에 대조가 된다.
CREATE TABLE IF NOT EXISTS transactions (
    tx_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    district_code   TEXT NOT NULL REFERENCES districts(district_code),
    deal_type       TEXT NOT NULL CHECK (deal_type IN ('매매','전세','월세')),
    price           INTEGER NOT NULL,          -- 거래 금액 (만원). 전세=보증금
    monthly_rent    INTEGER DEFAULT 0,         -- 월세 (만원)
    area_m2         REAL,
    build_year      INTEGER,
    deal_date       TEXT,                      -- YYYY-MM-DD
    raw_ref         TEXT,
    source          TEXT NOT NULL DEFAULT 'seoul_open_data'
                        CHECK (source IN ('seoul_open_data','molit_api','molit_csv','partner_agency')),
    collected_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 진단 대상 매물
CREATE TABLE IF NOT EXISTS properties (
    property_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    district_code   TEXT NOT NULL REFERENCES districts(district_code),
    address         TEXT NOT NULL,
    lat             REAL NOT NULL,
    lng             REAL NOT NULL,
    building_type   TEXT,
    complex_name    TEXT,                      -- CSV 실매물일 때만 채워진다
    area_m2         REAL,
    build_year      INTEGER,

    sale_price      INTEGER NOT NULL,          -- 추정 매매 시세 (만원)
    deposit         INTEGER NOT NULL,          -- 보증금 (만원)
    mortgage_amount INTEGER NOT NULL DEFAULT 0,-- 근저당 (만원)
    is_illegal      INTEGER NOT NULL DEFAULT 0,
    debt_known      INTEGER NOT NULL DEFAULT 1, -- 0이면 등기부 미연동이라 근저당 미확인

    source          TEXT NOT NULL DEFAULT 'partner_agency'
                        CHECK (source IN ('seoul_open_data','molit_api','molit_csv','iros_registry','partner_agency')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 진단 이력. 재진단하면 행이 쌓이고 아래 뷰가 최신 것만 뽑는다.
CREATE TABLE IF NOT EXISTS risk_assessments (
    assessment_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         INTEGER NOT NULL REFERENCES properties(property_id),
    jeonse_ratio        REAL,                  -- 보증금/매매가
    senior_debt_ratio   REAL,                  -- (근저당+보증금)/매매가
    fundamental_score   REAL,                  -- f (0~1)
    context_score       REAL,                  -- c (0~1)
    risk_score          INTEGER NOT NULL,      -- 100*(0.78f + 0.22c)
    risk_grade          TEXT NOT NULL,         -- 안전/주의/경고/위험
    recommended_product TEXT REFERENCES finance_products(product_code),
    proposal_rate_adjust REAL DEFAULT 0,       -- 음수면 우대(%p)
    match_reason         TEXT,
    assessed_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 금리·한도는 공개된 상품안내 기준 참고값. 변동금리 상품은 rate_min/max 가 NULL 이고
-- rate_asof 에 설명이 들어간다.
CREATE TABLE IF NOT EXISTS finance_products (
    product_code      TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    provider            TEXT NOT NULL,
    product_type        TEXT NOT NULL,
    guarantee_agency    TEXT,                   -- HF / HUG / SGI / 주택도시기금
    rate_min            REAL,
    rate_max            REAL,
    rate_asof           TEXT,
    max_loan_manwon     INTEGER,
    max_deposit_manwon  INTEGER,                -- NULL 이면 상한 없음
    target_grade        TEXT,
    description         TEXT,
    reference_url       TEXT
);

CREATE VIEW IF NOT EXISTS v_property_latest_risk AS
SELECT p.property_id, p.address, p.lat, p.lng, p.building_type,
       p.complex_name, p.area_m2, p.build_year,
       p.sale_price, p.deposit, p.mortgage_amount, p.is_illegal,
       p.debt_known, p.source,
       d.name AS district_name,
       d.jeonse_cv AS district_jeonse_cv,
       d.news_sentiment AS district_sentiment,
       r.jeonse_ratio, r.senior_debt_ratio,
       r.fundamental_score, r.context_score,
       r.risk_score, r.risk_grade,
       r.recommended_product, r.proposal_rate_adjust, r.match_reason
FROM properties p
JOIN districts d ON d.district_code = p.district_code
JOIN risk_assessments r ON r.property_id = p.property_id
WHERE r.assessment_id = (
    SELECT MAX(assessment_id) FROM risk_assessments r2 WHERE r2.property_id = p.property_id
);

CREATE INDEX IF NOT EXISTS idx_tx_district ON transactions(district_code);
CREATE INDEX IF NOT EXISTS idx_prop_district ON properties(district_code);
CREATE INDEX IF NOT EXISTS idx_risk_property ON risk_assessments(property_id);
