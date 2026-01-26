import csv
import json
import math
from datetime import datetime, timedelta, timezone
from io import StringIO

import requests

KST = timezone(timedelta(hours=9))

MY_TICKERS = ["NE","RXRX","BLDP","BMNR","NVDA","TSLA","AI","GGLL","QQQM","VRTL","CEVA","CCS"]

# Stooq: US 종목은 보통 "TICKER.US"
# 예: AAPL.US / 지수는 ^SPX, ^NDQ, ^DJI / 변동성은 VI.F / 달러인덱스 DX.F / WTI CL.F
def stooq_symbol(ticker: str) -> str:
    t = ticker.strip()
    if t.startswith("^") or t.endswith(".F") or t.endswith(".US"):
        return t
    return f"{t}.US"

def fetch_stooq_daily(symbol: str, days: int = 40):
    """
    Stooq CSV:
    https://stooq.com/q/d/l/?s=SYMBOL&d1=YYYYMMDD&d2=YYYYMMDD&i=d
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days * 3)  # 주말/휴장 감안 넉넉히
    d1 = start.strftime("%Y%m%d")
    d2 = end.strftime("%Y%m%d")

    url = "https://stooq.com/q/d/l/"
    params = {"s": symbol, "d1": d1, "d2": d2, "i": "d"}

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()

    # CSV columns: Date, Open, High, Low, Close, Volume
    rows = []
    reader = csv.DictReader(StringIO(r.text))
    for row in reader:
        if not row.get("Date") or not row.get("Close"):
            continue
        try:
            rows.append({
                "date": row["Date"],
                "close": float(row["Close"]) if row["Close"] not in ("", "nan") else None
            })
        except ValueError:
            continue

    rows = [x for x in rows if x["close"] is not None]
    rows.sort(key=lambda x: x["date"])
    return rows[-days:] if len(rows) > days else rows

def pct_change(last, prev):
    if last is None or prev is None or prev == 0:
        return None
    return (last / prev - 1.0) * 100.0

def fmt_price(last: float, digits=2):
    if last is None:
        return "-"
    return f"{last:,.{digits}f}"

def fred_last_value(series_id: str, days: int = 60):
    """
    FRED graph CSV (키 없이도 내려받기 가능):
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES
    """
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    r = requests.get(url, params={"id": series_id}, timeout=20)
    r.raise_for_status()
    reader = csv.reader(StringIO(r.text))
    next(reader, None)  # header
    data = []
    for d, v in reader:
        if v == "." or v == "":
            continue
        try:
            data.append((d, float(v)))
        except ValueError:
            continue
    data.sort(key=lambda x: x[0])
    tail = data[-days:] if len(data) > days else data
    return tail

def build_series_labels(values):
    # Chart labels는 최신 30개를 "YYYY-MM-DD"
    return [x["date"] for x in values]

def main():
    now_kst = datetime.now(KST)
    updated_at = now_kst.strftime("%Y-%m-%d %H:%M KST")

    # 1) 대표 지수/공포지수
    spx = fetch_stooq_daily("^SPX", 31)
    ndq = fetch_stooq_daily("^NDQ", 31)
    dji = fetch_stooq_daily("^DJI", 31)
    vix = fetch_stooq_daily("VI.F", 31)

    def last_and_change(series):
        last = series[-1]["close"] if series else None
        prev = series[-2]["close"] if len(series) >= 2 else None
        return last, pct_change(last, prev)

    spx_last, spx_chg = last_and_change(spx)
    ndq_last, ndq_chg = last_and_change(ndq)
    dji_last, dji_chg = last_and_change(dji)
    vix_last, vix_chg = last_and_change(vix)

    # 2) 매크로: 10Y(FRED), DXY(Stooq), WTI(Stooq)
    dgs10 = fred_last_value("DGS10", 31)  # (date, value)
    us10y_last = dgs10[-1][1] if dgs10 else None
    us10y_prev = dgs10[-2][1] if len(dgs10) >= 2 else None
    us10y_chg = pct_change(us10y_last, us10y_prev)

    dxy = fetch_stooq_daily("DX.F", 31)
    wti = fetch_stooq_daily("CL.F", 31)
    dxy_last, dxy_chg = last_and_change(dxy)
    wti_last, wti_chg = last_and_change(wti)

    # 3) 내 종목
    my_stocks = []
    for t in MY_TICKERS:
        sym = stooq_symbol(t)
        s = fetch_stooq_daily(sym, 10)
        last, chg = last_and_change(s)
        my_stocks.append({
            "symbol": t,
            "name": "",  # 무료로 이름까지 안정적으로 뽑는 건 귀찮아서 비움(원하면 매핑표 넣자)
            "last": fmt_price(last, 2) if last is not None else "-",
            "changePct": round(chg, 2) if chg is not None else None,
            "priceText": (f"{fmt_price(last,2)} ({'↑' if (chg or 0)>=0 else '↓'}{abs(chg):.2f}%)") if chg is not None else "-",
            "news": "",
            "nextEvent": "",
            "memo": ""
        })

    # 4) 차트용 series 만들기 (close 값만)
    def closes(series):
        return [x["close"] for x in series]

    macro_labels = [d for d, _ in dgs10[-30:]]
    macro_us10y = [v for _, v in dgs10[-30:]]

    # data/latest.json 스키마는 형님 대시보드 renderAll과 맞춤
    out = {
        "updatedAt": updated_at,

        # 여기 아래 텍스트들은 일단 "기본 템플릿"으로 두고,
        # 다음 단계에서 '룰 기반'으로 자동 생성해도 됨(완전 무료로 가능).
        "oneLine": "자동 업데이트(무료 데이터): 지수/금리/달러/유가 + 내 종목 변동만 우선 반영",
        "mood": {"value": "애매", "reason": "룰 기반 판정은 다음 단계에서 자동화"},
        "action": {"value": "관망", "note": "룰 기반 추천은 다음 단계에서 자동화", "beginnerMemo": "급할수록 손 떼는 게 이득일 때 많음."},

        "overnight": {
            "kpis": [
                {"icon":"📈","label":"S&P500","valueText":fmt_price(spx_last,2),"desc":"대표 지수","changePct": round(spx_chg,2) if spx_chg is not None else 0},
                {"icon":"📈","label":"나스닥","valueText":fmt_price(ndq_last,2),"desc":"기술주 비중","changePct": round(ndq_chg,2) if ndq_chg is not None else 0},
                {"icon":"📈","label":"다우","valueText":fmt_price(dji_last,2),"desc":"대형 가치주","changePct": round(dji_chg,2) if dji_chg is not None else 0},
                {"icon":"😱","label":"VIX","valueText":fmt_price(vix_last,2),"desc":"불안하면 ↑","changePct": round(vix_chg,2) if vix_chg is not None else 0},
            ],
            "bigFlowReason": "무료 자동화 v1: 큰 흐름 문장은 다음 단계에서 룰 기반으로 자동 생성",
            "series": {
                "labels": build_series_labels(spx[-30:]),
                "spx": closes(spx[-30:]),
                "ixic": closes(ndq[-30:]),
                "dji": closes(dji[-30:])
            }
        },

        "schedule": {"econ": [], "fed": []},

        "macro": {
            "kpis": [
                {"icon":"🏦","label":"미국 10년 금리","valueText": (f"{us10y_last:.2f}%" if us10y_last is not None else "-"),
                 "desc":"FRED(DGS10)","changePct": round(us10y_chg,2) if us10y_chg is not None else 0},
                {"icon":"💵","label":"달러값(DXY)","valueText": fmt_price(dxy_last,3) if dxy_last is not None else "-",
                 "desc":"Stooq(DX.F)","changePct": round(dxy_chg,2) if dxy_chg is not None else 0},
                {"icon":"🛢️","label":"유가(WTI)","valueText": (f"${fmt_price(wti_last,2)}" if wti_last is not None else "-"),
                 "desc":"Stooq(CL.F)","changePct": round(wti_chg,2) if wti_chg is not None else 0},
            ],
            "series": {
                "labels": macro_labels,
                "us10y": macro_us10y,
                "dxy": closes(dxy[-30:]),
                "wti": closes(wti[-30:])
            }
        },

        "newsTop5": [],
        "earnings": {"upcoming": [], "movers": []},
        "sectors": [],
        "myStocks": my_stocks,
        "risk": {"speed":"-", "vol":"-", "rule":"-"},
        "todo3": [
            "내 종목 변동 상위/하위 3개만 따로 체크",
            "급등/급락 종목은 뉴스 확인 후 대응",
            "오늘은 ‘한 번만’ 매매 규칙 지키기"
        ]
    }

    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
