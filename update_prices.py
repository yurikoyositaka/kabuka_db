"""プライム市場の株価を、相場が開いている日の大引け後にTursoへ更新する(GitHub Actions用)。"""
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
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
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
    """JPXの銘柄一覧からプライム銘柄を取得。失敗したらDBに入っている銘柄を使う。"""
    try:
        r = requests.get(JPX_URL, timeout=60)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content), dtype=str)
        df = df[df["市場・商品区分"].str.startswith("プライム", na=False)]
        codes = df["コード"].str.strip().tolist()
        if len(codes) > 1000:
            return codes
        raise RuntimeError(f"銘柄数が少なすぎます: {len(codes)}")
    except Exception as e:
        print("JPX一覧の取得に失敗。DBの銘柄を使います:", e)
        res = turso([(f"SELECT DISTINCT ticker FROM {TABLE}", [])])
        rows = res[0]["response"]["result"]["rows"]
        return [r[0]["value"].removesuffix(".T") for r in rows]


# ---------- 株価取得 ----------
def _num(x):
    return None if pd.isna(x) else float(x)


def wait_until_ready(today: str) -> bool:
    """当日分の株価がYahooに反映されるまで最大約30分待つ。"""
    for attempt in range(4):
        df = yf.download(REFERENCE, period="5d", interval="1d", progress=False, auto_adjust=True)
        if today in {d.strftime("%Y-%m-%d") for d in df.index}:
            return True
        if attempt < 3:
            print("当日データ未反映。10分待ちます...")
            time.sleep(600)
    return False


def fetch(tickers):
    rows = []
    for i in range(0, len(tickers), CHUNK):
        chunk = [t + ".T" for t in tickers[i:i + CHUNK]]
        df = yf.download(
            chunk, period=LOOKBACK, interval="1d", group_by="ticker",
            auto_adjust=AUTO_ADJUST, threads=True, progress=False,
        )
        for t in chunk:
            try:
                d = df[t].dropna(subset=["Close"])
            except KeyError:
                continue
            code = t if STORE_WITH_SUFFIX else t.removesuffix(".T")
            for idx, r in d.iterrows():
                vol = 0 if pd.isna(r["Volume"]) else int(r["Volume"])
                rows.append((
                    code, idx.strftime("%Y-%m-%d"),
                    _num(r["Open"]), _num(r["High"]), _num(r["Low"]), _num(r["Close"]), vol,
                ))
        time.sleep(1)
    return rows


# ---------- メイン ----------
def main():
    today = dt.datetime.now(JST).date()
    force = os.environ.get("FORCE") == "true"

    if not force and not is_market_day(today):
        print(f"{today} は休場日なので終了します")
        return
    if not force and not wait_until_ready(today.isoformat()):
        raise RuntimeError("当日データを取得できませんでした(臨時休場、またはデータ未反映)")

    tickers = get_tickers()
    rows = fetch(tickers)
    if not rows:
        raise RuntimeError("取得できたデータが0件でした")
    save(rows)

    msg = f"株価更新完了 {today}: 対象{len(tickers)}銘柄 / {len(rows)}行を保存"
    print(msg)
    notify("✅ " + msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify(f"❌ 株価更新に失敗: {e}")
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
