"""プライム市場の株価＋日経平均を、相場が開いている日の大引け後にTursoへ更新する(GitHub Actions用)。

・DBの最新日付から自動で続きを取得する(取りこぼした日があっても次回で埋まる)
・休場日(土日・祝日・年末年始)は何もせず終了
・当日分がまだyfinanceに出ていなければ、しばらく待ってから再確認する
"""
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
OVERLAP_DAYS = 7            # DBの最新日付から何日さかのぼって上書きするか(取りこぼし対策)
CHUNK = 200                 # yfinanceに一度に投げる銘柄数
REFERENCE = "7203.T"        # 当日データが出たかの確認に使う銘柄
INDEX_TICKERS = ["^N225"]   # 一緒に保存する指数(日経平均)
WAIT_TRIES = 4              # 当日データ未反映時の再確認回数
WAIT_SEC = 10 * 60          # 再確認の間隔(秒)
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
    print(msg)
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


def query_rows(sql, args=()):
    res = turso([(sql, list(args))])
    return res[0]["response"]["result"]["rows"]


def _cell(c):
    return c.get("value") if isinstance(c, dict) else c


def save(rows) -> None:
    for i in range(0, len(rows), 500):
        turso([(UPSERT, list(r)) for r in rows[i:i + 500]])


# ---------- 銘柄名の変換 ----------
def to_db_ticker(yf_ticker: str) -> str:
    if yf_ticker.startswith("^") or STORE_WITH_SUFFIX:
        return yf_ticker
    return yf_ticker.removesuffix(".T")


def to_yf_ticker(db_ticker: str) -> str:
    if db_ticker.startswith("^") or db_ticker.endswith(".T"):
        return db_ticker
    return db_ticker + ".T"


# ---------- 銘柄一覧 ----------
def get_tickers():
    """JPXの銘柄一覧からプライム銘柄を取得し、日経平均(^N225)を追加。失敗時はDBから。"""
    try:
        r = requests.get(JPX_URL, timeout=60)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content), dtype=str, engine="openpyxl")
        df = df[df["市場・商品区分"].str.startswith("プライム", na=False)]
        codes = df["コード"].str.strip().str.split(".").str[0].str.zfill(4).tolist()
        tickers = [c + ".T" for c in codes]
        if len(tickers) < 1000:
            raise RuntimeError(f"銘柄数が少なすぎます: {len(tickers)}")
    except Exception as e:
        print("JPX一覧の取得に失敗。DBの銘柄を使います:", e)
        rows = query_rows(f"SELECT DISTINCT ticker FROM {TABLE}")
        tickers = [to_yf_ticker(_cell(r[0])) for r in rows]

    # 日経平均などの指数を追加(重複は除く)
    for t in INDEX_TICKERS:
        if t not in tickers:
            tickers.append(t)
    tickers = [t for t in tickers if t.startswith("^")] + sorted(t for t in tickers if not t.startswith("^"))
    return tickers


# ---------- 取得開始日 ----------
def get_start_date(today: dt.date) -> dt.date:
    """環境変数 START_DATE があればそれを使う。なければ DBの最新日付 - OVERLAP_DAYS。"""
    s = (os.environ.get("START_DATE") or "").strip()
    if s:
        return dt.date.fromisoformat(s)
    try:
        rows = query_rows(f"SELECT MAX(date) FROM {TABLE} WHERE ticker = ?", [to_db_ticker(REFERENCE)])
        last = _cell(rows[0][0]) if rows else None
        if last:
            print("DBの最新日付:", last)
            return dt.date.fromisoformat(str(last)[:10]) - dt.timedelta(days=OVERLAP_DAYS)
    except Exception as e:
        print("DBの最新日付の取得に失敗:", e)
    return today - dt.timedelta(days=14)


# ---------- yfinance ----------
def download(tickers, start: dt.date, end: dt.date) -> pd.DataFrame:
    """end は含まない(yfinanceの仕様)。"""
    for attempt in range(3):
        try:
            return yf.download(
                tickers, start=start.isoformat(), end=end.isoformat(),
                auto_adjust=AUTO_ADJUST, group_by="ticker",
                threads=True, progress=False,
            )
        except Exception as e:
            print(f"yfinance取得エラー({attempt + 1}回目):", e)
            time.sleep(30)
    return pd.DataFrame()


def to_rows(df: pd.DataFrame, tickers) -> list:
    rows = []
    if df is None or df.empty:
        return rows
    if not isinstance(df.columns, pd.MultiIndex):  # 念のため(1銘柄だけのとき)
        df = pd.concat({tickers[0]: df}, axis=1)
    available = set(df.columns.get_level_values(0))
    for t in tickers:
        if t not in available:
            continue
        sub = df[t].dropna(subset=["Close"])
        for idx, r in sub.iterrows():
            vol = r.get("Volume")
            rows.append((
                to_db_ticker(t),
                pd.Timestamp(idx).strftime("%Y-%m-%d"),
                None if pd.isna(r["Open"]) else float(r["Open"]),
                None if pd.isna(r["High"]) else float(r["High"]),
                None if pd.isna(r["Low"]) else float(r["Low"]),
                float(r["Close"]),
                None if vol is None or pd.isna(vol) else int(vol),
            ))
    return rows


def latest_date_of(ticker: str, today: dt.date):
    df = download([ticker], today - dt.timedelta(days=10), today + dt.timedelta(days=1))
    rows = to_rows(df, [ticker])
    return max((r[1] for r in rows), default=None)


def wait_for_today(today: dt.date) -> bool:
    """当日データがyfinanceに出るまで待つ。出たら True。"""
    target = today.isoformat()
    for i in range(WAIT_TRIES + 1):
        latest = latest_date_of(REFERENCE, today)
        print(f"{REFERENCE} の最新日付: {latest}")
        if latest == target:
            return True
        if i < WAIT_TRIES:
            print(f"当日データ未反映。{WAIT_SEC // 60}分後に再確認します")
            time.sleep(WAIT_SEC)
    return False


# ---------- メイン ----------
def main() -> int:
    now = dt.datetime.now(JST)
    today = now.date()
    force = os.environ.get("FORCE", "").strip().lower() == "true"

    if not is_market_day(today) and not force:
        print(f"{today} は休場日のため終了します")
        return 0

    today_ready = wait_for_today(today) if is_market_day(today) else False

    start = get_start_date(today)
    end = today + dt.timedelta(days=1)
    tickers = get_tickers()
    print(f"取得期間: {start} 〜 {today} / 銘柄数: {len(tickers)}")

    total, got = 0, set()
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        rows = to_rows(download(chunk, start, end), chunk)
        save(rows)
        total += len(rows)
        got.update(r[0] for r in rows)
        print(f"  {i + len(chunk)}/{len(tickers)} 銘柄処理 ({len(rows)}行)")
        time.sleep(2)

    rows = query_rows(f"SELECT MAX(date), COUNT(*) FROM {TABLE} WHERE date = (SELECT MAX(date) FROM {TABLE})")
    db_last, db_cnt = _cell(rows[0][0]), _cell(rows[0][1])
    missing = len(tickers) - len(got)

    msg = (f"📈 株価更新 {today}\n"
           f"期間 {start}〜{today} / {len(got)}銘柄 {total}行を保存\n"
           f"DB最新日付: {db_last} ({db_cnt}銘柄)")
    if missing:
        msg += f"\n取得できなかった銘柄: {missing}"
    if is_market_day(today) and not today_ready:
        msg += f"\n⚠ {today} の当日データはまだyfinanceに出ていませんでした(次回実行で埋まります)"
    notify(msg)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify(f"❌ 株価更新でエラー: {e}")
        raise
