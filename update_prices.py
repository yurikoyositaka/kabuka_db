"""プライム市場の株価＋日経平均を、相場が開いている日の大引け後にTursoへ更新する(GitHub Actions用)。"""
import datetime as dt
import io
import os
import sys
import time
from zoneinfo import ZoneInfo

import jpholiday
import pandas as pd
import requests
import yfinance as yf

# ===== 設定(自分のDBに合わせて変更) =====
TABLE = "daily_prices"      # テーブル名。列: ticker, date, open, high, low, close, volume
STORE_WITH_SUFFIX = True    # True: tickerが "1301.T" 形式 / False: "1301" 形式
AUTO_ADJUST = True          # True: 分割調整後の株価で保存
LOOKBACK = "5d"             # 取りこぼし対策で、毎回直近5営業日分を上書き
CHUNK = 200                 # yfinanceに一度に投げる銘柄数
REFERENCE = "7203.T"        # 当日データが出たかの確認に使う銘柄
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
# =========================================

JST = ZoneInfo("Asia/Tokyo")

UPSERT = f"""INSERT INTO {TABLE} (ticker, date, open, high, low, close, volume)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker, date) DO UPDATE SET
  open = excluded.open, high = excluded.high, low = excluded.low,
  close = excluded.close, volume = excluded.volume"""


# ---------- 営業日判定 ----------
def is_market_day(d: dt.date) -> bool:
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    return (d.month, d.day) not in [(12, 31), (1, 1), (1, 2), (1, 3)]  # 東証の年末年始休場


# ---------- Discord通知(任意) ----------
def notify(msg: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": msg[:1900]}, timeout=15)
    except Exception as e:
        print("Discord通知に失敗:", e)


# ---------- Turso(HTTP API) ----------
def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def turso(statements):
    """statements: [(sql, [args...]), ...] をまとめて1回のリクエストで実行"""
    base = os.environ["TURSO_DATABASE_URL"].strip().replace("libsql://", "https://", 1)
    reqs = [
        {"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}
        for sql, args in statements
    ]
    reqs.append({"type": "close"})
    r = requests.post(
        base.rstrip("/") + "/v2/pipeline",
        headers={"Authorization": "Bearer " + os.environ["TURSO_AUTH_TOKEN"].strip()},
        json={"requests": reqs},
        timeout=120,
    )
    r.raise_for_status()
    results = r.json()["results"]
    for res in results:
        if res.get("type") == "error":
            raise RuntimeError(f"Tursoエラー: {res['error']}")
    return results


def save(rows) -> None:
    for i in range(0, len(rows), 500):
        turso([(UPSERT, list(r)) for r in rows[i:i + 500]])


# ---------- 銘柄一覧 ----------
def get_tickers():
    """JPXの銘柄一覧からプライム銘柄を取得し、日経平均(^N225)を追加。失敗時はDBから。"""
    try:
        r = requests.get(JPX_URL, timeout=60)
        r.raise_for_status()
        # .xlsx に対応するため engine='openpyxl' を指定
        df = pd.read_excel(io.BytesIO(r.content), dtype=str, engine='openpyxl')
        df = df[df["市場・商品区分"].str.startswith("プライム", na=False)]
        
        # yfinance用に末尾に.Tを付与したリストを作成
        codes = df["コード"].str.strip().str.split('.').str[0].str.zfill(4).tolist()
        yf_tickers = [c + ".T" for c in codes]
        
        # ★ ここで日経平均を追加 ★
        yf_tickers.append("^N225")
        
        if len(yf_tickers) > 1000:
            return yf_tickers
        raise RuntimeError(f"銘柄数が少なすぎます: {len(yf_tickers)}")
        
    except Exception as e:
        print("JPX一覧の取得に失敗。DBの銘柄を使います:", e)
        res = turso([(f"SELECT DISTINCT ticker FROM {TABLE}", [])])
        rows = res[0]["response"]["result"]["rows"]
        
        yf_tickers = []
        for r in rows:
            ticker = r[0]["value"]
            # インデックス（^N225等）はそのまま、個別株は必要に応じて.Tを補完
            if ticker.startswith("^"):
                yf_tickers.append(ticker)
            else:
                yf_tickers.append
