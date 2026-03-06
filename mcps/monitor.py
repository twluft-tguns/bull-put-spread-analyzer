#!/usr/bin/env python3
"""
Background monitor for Bull Put Spread Analyzer.
Runs during market hours (9:30 AM–4:00 PM ET, weekdays). Exits at market close so Task Scheduler
can start it again next morning. Sends Telegram alerts when a saved trade recommends
"Close Now" or "Close Now or Roll". Uses the same token file as the app.

Setup:
  1. Log in once via the Streamlit app so schwab_token.json exists in project root.
  2. Create a .env file in project root (see .env.example) with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     SCHWAB_CLIENT_ID, SCHWAB_CLIENT_SECRET. Optional: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
  3. Run: python -m mcps.monitor   (or use run_monitor.bat + Task Scheduler for automatic runs)

Alerts are rate-limited to once per trade per 30 minutes.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Load .env from project root so Task Scheduler / batch run don't need to set env vars
_project_root = Path(__file__).resolve().parent.parent
_env_file = _project_root / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass

# Use same token file as the Streamlit app
SCHWAB_TOKEN_FILE = Path(os.environ.get("SCHWAB_TOKEN_FILE", "schwab_token.json"))
TRADES_FILE = Path(os.environ.get("TRADES_FILE", "saved_trades.json"))
LAST_ALERT_FILE = Path(os.environ.get("LAST_ALERT_FILE", "monitor_last_alert.json"))
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "5"))
ALERT_COOLDOWN_SECONDS = 30 * 60  # 30 min per trade


def load_token() -> dict | None:
    if not SCHWAB_TOKEN_FILE.exists():
        return None
    try:
        with open(SCHWAB_TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def refresh_token() -> dict:
    token = load_token()
    if not token or not token.get("refresh_token"):
        raise RuntimeError("No refresh token. Log in once via the Streamlit app.")
    client_id = os.environ.get("SCHWAB_CLIENT_ID")
    client_secret = os.environ.get("SCHWAB_CLIENT_SECRET")
    token_url = os.environ.get("SCHWAB_TOKEN_URL", "https://api.schwabapi.com/v1/oauth/token")
    if not client_id or not client_secret:
        raise RuntimeError("Set SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET (or use app secrets).")
    credentials = f"{client_id}:{client_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()
    headers = {"Authorization": f"Basic {encoded}"}
    data = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
    resp = requests.post(token_url, data=data, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text[:300]}")
    new_token = resp.json()
    if not new_token.get("refresh_token"):
        new_token["refresh_token"] = token.get("refresh_token")
    new_token["_obtained_at"] = datetime.datetime.now(datetime.timezone.utc).timestamp()
    try:
        with open(SCHWAB_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(new_token, f)
    except Exception:
        pass
    return new_token


def get_access_token() -> str:
    token = load_token()
    if not token or not token.get("access_token"):
        raise RuntimeError("No token file. Log in once via the Streamlit app.")
    expires_in = token.get("expires_in", 0)
    obtained = token.get("_obtained_at") or 0
    if obtained and expires_in:
        if (datetime.datetime.now(datetime.timezone.utc).timestamp() - obtained) >= (expires_in - 60):
            token = refresh_token()
    return token["access_token"]


def fetch_schwab_live_data(
    access_token: str,
    ticker: str,
    expiration_date: datetime.date,
    short_put_strike: float,
    long_put_strike: float,
) -> dict:
    """Call Schwab chains API; return current_price, current_debit_to_close, net_delta, net_theta, net_vega, current_iv."""
    base = "https://api.schwabapi.com/marketdata/v1/chains"
    exp_str = expiration_date.strftime("%Y-%m-%d")
    params = {
        "symbol": ticker.upper(),
        "contractType": "PUT",
        "fromDate": exp_str,
        "toDate": exp_str,
        "includeUnderlyingQuote": "true",
        "strikeCount": "20",
    }
    headers = {"Authorization": f"Bearer {access_token}", "Schwab-Client-CorrelId": str(uuid.uuid4())}
    resp = requests.get(base, params=params, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Chains request failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    put_map = data.get("putExpDateMap") or {}
    exp_key = next((k for k in put_map if k.startswith(exp_str)), None)
    if not exp_key:
        raise RuntimeError(f"No put chain for {exp_str}.")
    strikes_map = put_map[exp_key]

    def get_contract(strike: float):
        for skey, val in strikes_map.items():
            try:
                if abs(float(skey) - strike) < 0.01:
                    if isinstance(val, list) and val:
                        return val[0]
                    return val
            except (TypeError, ValueError):
                continue
        return None

    def quote(c):
        if isinstance(c, dict) and isinstance(c.get("quote"), dict):
            return c["quote"]
        return c

    def f(obj, key, default=0.0):
        v = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    short_c = get_contract(short_put_strike)
    long_c = get_contract(long_put_strike)
    if not short_c or not long_c:
        raise RuntimeError("Strikes not found in chain.")
    short_c = short_c if isinstance(short_c, dict) else {}
    long_c = long_c if isinstance(long_c, dict) else {}

    def get_price(c, ask_not_bid):
        q = quote(c)
        if ask_not_bid:
            return f(q, "askPrice") or f(q, "markPrice") or f(q, "mark") or f(q, "lastPrice") or f(q, "closePrice")
        return f(q, "bidPrice") or f(q, "markPrice") or f(q, "mark") or f(q, "lastPrice") or f(q, "closePrice")

    short_ask = get_price(short_c, True)
    long_bid = get_price(long_c, False)
    debit_to_close = max(short_ask - long_bid, 0.0)
    if debit_to_close == 0:
        debit_to_close = max(
            f(quote(short_c), "markPrice") or 0 - f(quote(long_c), "markPrice") or 0,
            0.0,
        )

    def greek(c, name):
        q = quote(c)
        val = f(q, name) or f(c, name)
        return val or 0.0

    net_delta = -greek(short_c, "delta") + greek(long_c, "delta")
    net_theta = -greek(short_c, "theta") + greek(long_c, "theta")
    net_vega = -greek(short_c, "vega") + greek(long_c, "vega")
    short_iv = greek(short_c, "volatility")
    current_iv = short_iv * 100 if 0 < short_iv < 2 else short_iv
    underlying = data.get("underlying") or {}
    q = quote(underlying) if isinstance(underlying, dict) else underlying
    current_price = f(q, "lastPrice") or f(q, "markPrice") or f(q, "mark") or 0.0
    if current_price <= 0 and data.get("underlyingPrice") is not None:
        try:
            current_price = float(data["underlyingPrice"])
        except (TypeError, ValueError):
            pass

    return {
        "current_price": round(current_price, 2),
        "current_debit_to_close": round(debit_to_close, 2),
        "net_delta": round(net_delta, 2),
        "net_theta": round(net_theta, 2),
        "net_vega": round(net_vega, 2),
        "current_iv": round(current_iv, 2),
    }


def get_recommendation(
    dte: int,
    profit_pct: float,
    current_profit: float,
    net_delta: float,
    net_theta: float,
    iv_change: float,
    price_near_short: bool,
) -> str:
    """Return recommendation string (e.g. '✅ Close Now')."""
    from mcps.bull_put_analyzer import get_recommendation as _rec

    rec, _, _ = _rec(dte, profit_pct, current_profit, net_delta, net_theta, iv_change, price_near_short)
    return rec


def get_all_trades() -> list:
    """Return list of (workspace_key, label, payload) for all saved trades."""
    result = []
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if supabase_url and supabase_key:
        try:
            from supabase import create_client
            sb = create_client(supabase_url, supabase_key)
            resp = sb.table("trades").select("owner_key,label,data").order("updated_at", desc=True).execute()
            for r in (resp.data or []):
                key = (r.get("owner_key") or "").strip()
                label = (r.get("label") or "").strip()
                data = r.get("data") or {}
                if key and label and isinstance(data, dict) and data.get("ticker"):
                    result.append((key, label, data))
            if result:
                return result
        except Exception:
            pass
    if not TRADES_FILE.exists():
        return result
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            all_trades = json.load(f)
    except Exception:
        return result
    if not all_trades:
        return result
    for wk, v in all_trades.items():
        if isinstance(v, dict):
            for label, payload in v.items():
                if isinstance(payload, dict) and payload.get("ticker"):
                    result.append((wk, label, payload))
    if all(isinstance(v, dict) and v.get("ticker") for v in all_trades.values()):
        for label, payload in all_trades.items():
            result.append(("default", label, payload))
    return result


def load_last_alert() -> dict:
    if not LAST_ALERT_FILE.exists():
        return {}
    try:
        with open(LAST_ALERT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_last_alert(key: str) -> None:
    data = load_last_alert()
    data[key] = time.time()
    try:
        with open(LAST_ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def run_once(access_token: str) -> None:
    from mcps.bull_put_analyzer import (
        compute_dte,
        compute_profit_metrics,
        compute_iv_change,
        is_price_near_short_strike,
    )
    last = load_last_alert()
    now = time.time()
    for workspace_key, label, payload in get_all_trades():
        ticker = (payload.get("ticker") or "").strip().upper() or "SPY"
        try:
            exp_val = payload.get("expiration_date")
            if isinstance(exp_val, str):
                exp = datetime.date.fromisoformat(exp_val)
            else:
                exp = exp_val or (datetime.date.today() + datetime.timedelta(days=30))
        except Exception:
            continue
        short_s = float(payload.get("short_put_strike") or 0)
        long_s = float(payload.get("long_put_strike") or 0)
        if short_s <= 0 or long_s <= 0:
            continue
        try:
            live = fetch_schwab_live_data(access_token, ticker, exp, short_s, long_s)
        except Exception:
            continue
        entry_credit = float(payload.get("entry_credit") or 0)
        iv_entry = float(payload.get("iv_at_entry") or 0)
        if not entry_credit:
            continue
        dte = compute_dte(exp)
        current_profit, profit_pct = compute_profit_metrics(entry_credit, live["current_debit_to_close"])
        iv_change = compute_iv_change(live["current_iv"], iv_entry)
        near_short = is_price_near_short_strike(live["current_price"], short_s)
        rec = get_recommendation(
            dte, profit_pct, current_profit,
            live["net_delta"], live["net_theta"], iv_change, near_short,
        )
        if rec not in ("✅ Close Now", "⚠️ Close Now or Roll"):
            continue
        alert_key = f"{workspace_key}|{label}"
        if (now - last.get(alert_key, 0)) < ALERT_COOLDOWN_SECONDS:
            continue
        msg = f"Bull Put Spread Alert – {rec}\nTrade: {label} ({ticker} {short_s}/{long_s})\nProfit: {profit_pct:.1f}% | DTE: {dte}"
        if send_telegram(msg):
            save_last_alert(alert_key)
            print(f"Alert sent: {label}")


def _now_et() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo("America/New_York"))


def _is_market_hours(now_et: datetime.datetime) -> bool:
    """True if weekday and between 9:30 AM and 4:00 PM ET."""
    if now_et.weekday() >= 5:  # Saturday, Sunday
        return False
    if now_et.hour < 9:
        return False
    if now_et.hour == 9 and now_et.minute < 30:
        return False
    if now_et.hour >= 16:
        return False
    return True


def _sleep_until_market_open(now_et: datetime.datetime) -> None:
    """Sleep until next 9:30 AM ET (weekday)."""
    target = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et >= target:
        target += datetime.timedelta(days=1)
    while target.weekday() >= 5:
        target += datetime.timedelta(days=1)
    delta = target - _now_et()
    secs = max(0, int(delta.total_seconds()))
    if secs > 0:
        print(f"Market closed. Next open: {target.strftime('%Y-%m-%d %H:%M')} ET. Sleeping {secs}s.")
        time.sleep(secs)


def main() -> None:
    print("Bull Put Spread Monitor – Telegram alerts for Close Now. Runs market hours (9:30 AM–4 PM ET) only. Ctrl+C to stop.")
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env or environment.")
    while True:
        now_et = _now_et()
        if not _is_market_hours(now_et):
            if now_et.hour >= 16 or now_et.weekday() >= 5:
                print(f"Market closed ({now_et.strftime('%Y-%m-%d %H:%M')} ET). Exiting until next run.")
                break
            _sleep_until_market_open(now_et)
            continue
        try:
            token = get_access_token()
            run_once(token)
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
