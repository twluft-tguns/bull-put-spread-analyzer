import base64
import datetime
import time
import uuid
from typing import List, Tuple
import json
from pathlib import Path
from urllib.parse import urlencode

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

TRADES_FILE = Path("saved_trades.json")
# Persist Schwab OAuth token so user stays logged in across refreshes
SCHWAB_TOKEN_FILE = Path("schwab_token.json")


def compute_dte(expiration: datetime.date) -> int:
    today = datetime.date.today()
    return (expiration - today).days


def compute_profit_metrics(entry_credit: float, current_debit: float) -> Tuple[float, float]:
    if entry_credit is None or current_debit is None:
        return 0.0, 0.0
    current_profit = entry_credit - current_debit
    profit_pct = (current_profit / entry_credit * 100) if entry_credit else 0.0
    return current_profit, profit_pct


def compute_iv_change(current_iv: float, entry_iv: float) -> float:
    if current_iv is None or entry_iv is None:
        return 0.0
    return current_iv - entry_iv


def is_price_near_short_strike(underlying: float, short_strike: float, tolerance_pct: float = 1.0) -> bool:
    if underlying is None or short_strike is None or short_strike == 0:
        return False
    diff_pct = abs(underlying - short_strike) / short_strike * 100
    return diff_pct <= tolerance_pct


def get_recommendation(
    dte: int,
    profit_pct: float,
    current_profit: float,
    net_delta: float,
    net_theta: float,
    iv_change: float,
    price_near_short: bool,
) -> Tuple[str, str, List[str]]:
    reasons: List[str] = []
    close_signals: List[str] = []

    theta_threshold = 1.0  # very low daily decay threshold (in $ per day)
    iv_rise_threshold = 5.0  # percentage points

    losing_money = profit_pct < 0

    # Rule: Close if profit ≥ 80%
    if profit_pct >= 80:
        close_signals.append("High profit (≥ 80%) – good time to lock in gains.")

    # Rule: Close if profit ≥ 50% AND DTE ≤ 21
    if profit_pct >= 50 and dte <= 21:
        close_signals.append("Solid profit (≥ 50%) with ≤ 21 DTE – risk/reward no longer favorable.")

    # Rule: Close if DTE < 7 (avoid gamma risk)
    if dte < 7:
        close_signals.append("Very low DTE (< 7) – elevated gamma risk near expiration.")

    # Rule: Close if |Net Delta| > 0.50 (too directional)
    if abs(net_delta) > 0.50:
        close_signals.append(f"Net delta |{net_delta:.2f}| > 0.50 – spread is too directional.")

    # Rule: Close if Net Theta is very low (almost no daily decay left)
    if abs(net_theta) < theta_threshold:
        close_signals.append(
            f"Net theta is very low ({net_theta:.2f}) – limited additional time decay benefit remaining."
        )

    # Rule: Close if IV has risen more than 5%
    if iv_change >= iv_rise_threshold:
        close_signals.append(
            f"Implied volatility has increased by {iv_change:.2f}% (≥ {iv_rise_threshold}%) – risk has increased."
        )

    # Rule: Close if losing money and price is near the short strike
    if losing_money and price_near_short:
        close_signals.append(
            "Currently losing money and price is near the short strike – risk of assignment or further losses."
        )

    # Aggregate recommendation
    if close_signals:
        # Distinguish between locking gains vs. defensive close
        if current_profit >= 0:
            recommendation = "✅ Close Now"
            color = "#16a34a"  # green
        else:
            recommendation = "⚠️ Close Now or Roll"
            color = "#f97316"  # orange
        reasons.extend(close_signals)
    else:
        # No strong close signal – decide between mild hold and monitor
        if 0 <= profit_pct < 50 and dte > 7:
            recommendation = "🟢 Hold 3–7 more days"
            color = "#22c55e"  # bright green
            reasons.append(
                "No strong risk signals detected and there is still time value – holding a few more days is reasonable."
            )
        else:
            recommendation = "🟡 Hold and Monitor"
            color = "#eab308"  # yellow
            reasons.append(
                "No explicit close trigger, but monitor price, volatility, and greeks closely as conditions can change."
            )

    # Add a concise summary reason based on high-level state
    if dte < 0:
        reasons.append("Warning: Expiration date is in the past – verify the inputs.")
    else:
        reasons.append(f"DTE: {dte} days, Profit: {profit_pct:.1f}%, IV change: {iv_change:.1f}%.")

    return recommendation, color, reasons


def render_recommendation_box(recommendation: str, color: str, reasons: List[str]):
    box_style = f"""
    <div style="
        border-radius: 10px;
        padding: 1.2rem 1.4rem;
        background-color: {color}20;
        border-left: 8px solid {color};
        margin-bottom: 1rem;
    ">
        <h3 style="margin: 0 0 0.5rem 0; color: {color}; font-weight: 700;">
            {recommendation}
        </h3>
        <ul style="margin: 0 0 0 1.2rem; padding-left: 0;">
    """
    for r in reasons:
        box_style += f"<li style='margin-bottom: 0.25rem; color: #1f2933;'>{r}</li>"
    box_style += """
        </ul>
    </div>
    """
    st.markdown(box_style, unsafe_allow_html=True)


def load_saved_trades() -> dict:
    if not TRADES_FILE.exists():
        return {}
    try:
        with TRADES_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_saved_trades(trades: dict) -> None:
    with TRADES_FILE.open("w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)


def _looks_like_trade_payload(v: object) -> bool:
    return isinstance(v, dict) and "ticker" in v and "short_put_strike" in v and "expiration_date" in v


def has_supabase_config() -> bool:
    try:
        cfg = st.secrets.get("supabase", {})
        return bool(cfg.get("url") and (cfg.get("service_role_key") or cfg.get("anon_key")))
    except Exception:
        return False


@st.cache_resource
def get_supabase_client():
    from supabase import create_client

    cfg = st.secrets["supabase"]
    key = cfg.get("service_role_key") or cfg.get("anon_key")
    return create_client(cfg["url"], key)


def list_trades(owner_key: str) -> dict:
    """
    Returns {label: payload_dict} for this owner_key.
    Uses Supabase if configured; otherwise falls back to local JSON.
    """
    if has_supabase_config():
        sb = get_supabase_client()
        resp = (
            sb.table("trades")
            .select("label,data")
            .eq("owner_key", owner_key)
            .order("updated_at", desc=True)
            .execute()
        )
        rows = resp.data or []
        return {r["label"]: r["data"] for r in rows}

    all_trades = load_saved_trades()
    # Back-compat: previously we stored {label: payload} (no workspace key)
    if all_trades and all(_looks_like_trade_payload(v) for v in all_trades.values()):
        return all_trades
    return all_trades.get(owner_key, {})


def upsert_trade(owner_key: str, label: str, payload: dict) -> None:
    if has_supabase_config():
        sb = get_supabase_client()
        sb.table("trades").upsert(
            {
                "owner_key": owner_key,
                "label": label,
                "data": payload,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            on_conflict="owner_key,label",
        ).execute()
        return

    all_trades = load_saved_trades()
    if all_trades and all(_looks_like_trade_payload(v) for v in all_trades.values()):
        # Legacy local format
        all_trades[label] = payload
        save_saved_trades(all_trades)
        return

    all_trades.setdefault(owner_key, {})
    all_trades[owner_key][label] = payload
    save_saved_trades(all_trades)


def delete_trade(owner_key: str, label: str) -> None:
    if has_supabase_config():
        sb = get_supabase_client()
        sb.table("trades").delete().eq("owner_key", owner_key).eq("label", label).execute()
        return

    all_trades = load_saved_trades()
    if all_trades and all(_looks_like_trade_payload(v) for v in all_trades.values()):
        if label in all_trades:
            del all_trades[label]
            save_saved_trades(all_trades)
        return

    if owner_key in all_trades and label in all_trades[owner_key]:
        del all_trades[owner_key][label]
        save_saved_trades(all_trades)


def list_workspace_keys() -> list:
    """
    Return workspace keys (owner_key) to show in dropdown, most recent first when possible.
    Supabase: distinct owner_key ordered by latest updated_at. Local: sorted keys.
    """
    if has_supabase_config():
        try:
            sb = get_supabase_client()
            resp = (
                sb.table("trades")
                .select("owner_key,updated_at")
                .order("updated_at", desc=True)
                .execute()
            )
            seen = set()
            keys = []
            for r in (resp.data or []):
                k = (r.get("owner_key") or "").strip()
                if k and k not in seen:
                    seen.add(k)
                    keys.append(k)
            return keys
        except Exception:
            pass
    all_trades = load_saved_trades()
    if not all_trades:
        return []
    if all_trades and all(_looks_like_trade_payload(v) for v in all_trades.values()):
        return []
    return sorted(all_trades.keys())


def ensure_workspace_key(known_keys: list | None = None) -> str:
    # Persist workspace key in URL so it survives reruns and worker changes.
    # Prefer existing session_state over URL. When nothing set, default to first known key.
    import secrets

    try:
        q = st.query_params
    except Exception:
        q = {}

    url_key = None
    if hasattr(q, "get"):
        url_key = (q.get("workspace_key") or "").strip()
    if isinstance(url_key, list):
        url_key = (url_key[0] or "").strip() if url_key else ""

    current = (st.session_state.get("workspace_key") or "").strip()
    if current:
        try:
            st.query_params["workspace_key"] = current
        except Exception:
            pass
        return current

    if url_key:
        st.session_state["workspace_key"] = url_key
        return url_key

    # No key in session or URL: default to first workspace key in list (e.g. most recent)
    keys = known_keys if known_keys is not None else list_workspace_keys()
    if keys:
        st.session_state["workspace_key"] = keys[0]
        try:
            st.query_params["workspace_key"] = keys[0]
        except Exception:
            pass
        return keys[0]

    new_key = secrets.token_urlsafe(16)
    st.session_state["workspace_key"] = new_key
    try:
        st.query_params["workspace_key"] = new_key
    except Exception:
        pass
    return new_key


def has_schwab_config() -> bool:
    try:
        cfg = st.secrets.get("schwab", {})
        return bool(cfg.get("client_id") and cfg.get("client_secret") and cfg.get("auth_url") and cfg.get("token_url"))
    except Exception:
        return False


def has_telegram_config() -> bool:
    try:
        cfg = st.secrets.get("telegram", {})
        return bool(cfg.get("bot_token") and cfg.get("chat_id"))
    except Exception:
        return False


def send_telegram_message(text: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not has_telegram_config():
        return False
    try:
        cfg = st.secrets["telegram"]
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def build_schwab_auth_url() -> str:
    cfg = st.secrets["schwab"]
    base = cfg["auth_url"]
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": "readonly",
    }
    return f"{base}?{urlencode(params)}"


def save_schwab_token(token_data: dict) -> None:
    """Persist token to file so login survives refresh and monitor can use it."""
    if not token_data or not token_data.get("access_token"):
        return
    payload = dict(token_data)
    payload["_obtained_at"] = datetime.datetime.now(datetime.timezone.utc).timestamp()
    try:
        with SCHWAB_TOKEN_FILE.open("w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def load_schwab_token() -> dict | None:
    """Load persisted token from file if present."""
    if not SCHWAB_TOKEN_FILE.exists():
        return None
    try:
        with SCHWAB_TOKEN_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def refresh_schwab_token() -> dict:
    """Use refresh_token to get new access_token; update session and file."""
    cfg = st.secrets["schwab"]
    token = st.session_state.get("schwab_token") or load_schwab_token()
    if not token or not token.get("refresh_token"):
        raise RuntimeError("No refresh token. Reconnect to Schwab.")
    credentials = f"{cfg['client_id']}:{cfg['client_secret']}"
    encoded = base64.b64encode(credentials.encode()).decode()
    headers = {"Authorization": f"Basic {encoded}"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
    }
    resp = requests.post(cfg["token_url"], data=data, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text[:300]}")
    new_token = resp.json()
    if not new_token.get("refresh_token"):
        new_token["refresh_token"] = token.get("refresh_token")
    st.session_state["schwab_token"] = new_token
    save_schwab_token(new_token)
    return new_token


def exchange_code_for_token(code: str) -> None:
    cfg = st.secrets["schwab"]
    # Schwab token endpoint expects client_id and client_secret via Basic auth, not in body
    credentials = f"{cfg['client_id']}:{cfg['client_secret']}"
    encoded = base64.b64encode(credentials.encode()).decode()
    headers = {"Authorization": f"Basic {encoded}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
    }
    resp = requests.post(cfg["token_url"], data=data, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Token request failed: {resp.status_code} {resp.text}")
    token_data = resp.json()
    st.session_state["schwab_token"] = token_data
    st.session_state.pop("schwab_auth_error", None)
    save_schwab_token(token_data)


def get_schwab_access_token() -> str:
    """Return current access_token; refresh if expired. Restore from file if not in session."""
    if "schwab_token" not in st.session_state:
        loaded = load_schwab_token()
        if loaded:
            st.session_state["schwab_token"] = loaded
    if "schwab_token" not in st.session_state:
        raise RuntimeError("Not connected to Schwab. Click 'Connect to Schwab' first.")
    token = st.session_state["schwab_token"]
    access = token.get("access_token")
    if not access:
        raise RuntimeError("Schwab token missing access_token. Reconnect to Schwab.")
    # Refresh if expired or expiring within 60s (Schwab access tokens ~30 min)
    expires_in = token.get("expires_in", 0)
    obtained = token.get("_obtained_at") or 0
    if obtained and expires_in and (datetime.datetime.now(datetime.timezone.utc).timestamp() - obtained) >= (expires_in - 60):
        token = refresh_schwab_token()
        access = token.get("access_token", access)
    return access


def fetch_schwab_live_data(
    ticker: str,
    expiration_date: datetime.date,
    short_put_strike: float,
    long_put_strike: float,
    access_token: str | None = None,
) -> dict:
    """
    Call Schwab marketdata chains API and return dict with:
    current_price, current_debit_to_close, net_delta, net_theta, net_vega, current_iv
    If access_token is provided (e.g. for background monitor), use it; else use session token.
    """
    token = access_token or get_schwab_access_token()
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
    headers = {
        "Authorization": f"Bearer {token}",
        "Schwab-Client-CorrelId": str(uuid.uuid4()),
    }
    resp = requests.get(base, params=params, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Chains request failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()

    # Parse: putExpDateMap -> { "expDate:period" : { "strike" : [ contract ] } }
    put_map = data.get("putExpDateMap") or {}
    # Find expiration key that matches our date (key often "YYYY-MM-DD:1")
    exp_key = None
    for k in put_map:
        if k.startswith(exp_str):
            exp_key = k
            break
    if not exp_key:
        raise RuntimeError(f"No put chain found for expiration {exp_str}. Check ticker and date.")
    strikes_map = put_map[exp_key]

    def get_contract(strike: float):
        # Strike can be "430.0", "430", or 430; value can be single contract or list of one
        for skey, val in strikes_map.items():
            try:
                if abs(float(skey) - strike) < 0.01:
                    if isinstance(val, list) and val:
                        return val[0]
                    if isinstance(val, dict) and not any(k in val for k in ("putCall", "symbol", "bidPrice", "bid")) and val:
                        # One more nesting level: strike -> { innerKey: contract }
                        for inner in val.values():
                            if isinstance(inner, dict) and (inner.get("putCall") or inner.get("bidPrice") is not None or inner.get("bid") is not None):
                                return inner
                            if isinstance(inner, list) and inner:
                                return inner[0]
                            break
                    return val
            except (TypeError, ValueError):
                continue
        return None

    short_c = get_contract(short_put_strike)
    long_c = get_contract(long_put_strike)
    if not short_c or not isinstance(short_c, dict):
        raise RuntimeError(f"Short put strike {short_put_strike} not found in chain.")
    if not long_c or not isinstance(long_c, dict):
        raise RuntimeError(f"Long put strike {long_put_strike} not found in chain.")

    # Chains API returns OptionContract: bidPrice, askPrice, lastPrice, markPrice, closePrice, delta, theta, vega, volatility at top level (no "quote" wrapper)
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

    # OptionContract: bidPrice, askPrice, markPrice, lastPrice, closePrice (use markPrice; QuoteOption uses "mark")
    def get_price(c, ask_not_bid):
        q = quote(c)
        if ask_not_bid:
            return f(q, "askPrice") or f(q, "markPrice") or f(q, "mark") or f(q, "lastPrice") or f(q, "closePrice")
        return f(q, "bidPrice") or f(q, "markPrice") or f(q, "mark") or f(q, "lastPrice") or f(q, "closePrice")

    short_bid = get_price(short_c, False)
    short_ask = get_price(short_c, True)
    long_bid = get_price(long_c, False)
    long_ask = get_price(long_c, True)
    debit_to_close = max((short_ask - long_bid), 0.0)
    if debit_to_close == 0:
        qs, ql = quote(short_c), quote(long_c)
        short_mid = f(qs, "markPrice") or f(qs, "mark") or f(qs, "lastPrice") or f(qs, "closePrice") or ((short_bid + short_ask) / 2 if (short_bid or short_ask) else 0)
        long_mid = f(ql, "markPrice") or f(ql, "mark") or f(ql, "lastPrice") or f(ql, "closePrice") or ((long_bid + long_ask) / 2 if (long_bid or long_ask) else 0)
        debit_to_close = max(short_mid - long_mid, 0.0)

    # OptionContract: delta, theta, vega, volatility at top level (no nested quote)
    def greek(c, name):
        q = quote(c)
        val = f(q, name)
        if val != 0.0:
            return val
        if q is not c:
            val = f(c, name)
        return val or 0.0

    short_delta = greek(short_c, "delta")
    short_theta = greek(short_c, "theta")
    short_vega = greek(short_c, "vega")
    long_delta = greek(long_c, "delta")
    long_theta = greek(long_c, "theta")
    long_vega = greek(long_c, "vega")
    # Net greeks for spread: -short + long. Keep per-contract scale (no * 100).
    # Delta is -1..1 per share; theta/vega are $ per day / $ per 1% IV per contract.
    net_delta = -short_delta + long_delta
    net_theta = -short_theta + long_theta
    net_vega = -short_vega + long_vega

    # IV: try both "volatility" and "impliedVolatility"; normalize to percent (API may return decimal 0.22 or percent 22)
    short_iv = greek(short_c, "volatility") or greek(short_c, "impliedVolatility")
    if short_iv and 0 < short_iv < 2:
        current_iv = short_iv * 100  # decimal (0.22) -> percent
    elif short_iv:
        current_iv = short_iv  # already percent
    else:
        current_iv = 0.0

    # OptionChain: underlyingPrice at root; underlying: Underlying{} may have lastPrice, markPrice, etc.
    underlying = data.get("underlying") or {}
    q = quote(underlying) if isinstance(underlying, dict) else underlying
    current_price = (
        f(q, "lastPrice") or f(q, "markPrice") or f(q, "mark") or f(q, "closePrice") or f(q, "askPrice") or 0.0
    )
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


def main():
    st.set_page_config(
        page_title="Bull Put Spread Analyzer – Optimal Exit Time",
        layout="wide",
        page_icon="📊",
    )

    # Handle Schwab OAuth redirect BEFORE widgets are created
    if has_schwab_config():
        try:
            params = st.query_params
        except Exception:
            params = {}
        code_values = params.get("code")
        if code_values and "schwab_token" not in st.session_state:
            code = code_values[0] if isinstance(code_values, list) else code_values
            try:
                exchange_code_for_token(code)
                # Clear code from URL on next run
                st.query_params.clear()
                st.session_state["auto_fetch_live_on_connect"] = True
            except Exception as e:
                st.session_state["schwab_auth_error"] = str(e)
        # Stay logged in: restore token from file if not in session
        if "schwab_token" not in st.session_state:
            loaded = load_schwab_token()
            if loaded and loaded.get("access_token"):
                st.session_state["schwab_token"] = loaded
                st.session_state["auto_fetch_live_on_connect"] = True

    # Apply any pending loaded trade data BEFORE widgets are created.
    # Do NOT restore live fields (current_price, current_iv, etc.) from the payload—they may
    # be from the wrong ticker (e.g. SPY). We refetch live data for the loaded trade's ticker.
    if "loaded_trade_data" in st.session_state:
        data = st.session_state.pop("loaded_trade_data")
        st.session_state["ticker"] = data["ticker"]
        st.session_state["short_put_strike"] = data["short_put_strike"]
        st.session_state["long_put_strike"] = data["long_put_strike"]
        exp_val = data["expiration_date"]
        if isinstance(exp_val, str):
            st.session_state["expiration_date"] = datetime.date.fromisoformat(exp_val)
        else:
            st.session_state["expiration_date"] = exp_val
        st.session_state["entry_credit"] = float(data.get("entry_credit") or 0.0)
        # Live data: clear so sidebar refetches for this ticker (avoids showing SPY data for AMZN, etc.)
        for key in ("current_price", "current_debit_to_close", "net_delta", "net_theta", "net_vega", "current_iv"):
            st.session_state.pop(key, None)
        st.session_state.pop("last_live_fetch_time", None)
        st.session_state["iv_at_entry"] = float(data.get("iv_at_entry") or 0.0)
        st.session_state["iv_at_entry_baseline"] = float(data.get("iv_at_entry") or 0.0)
        st.session_state["notes"] = data.get("notes") or ""

    if "ticker" not in st.session_state:
        st.session_state["ticker"] = "SPY"

    def _normalize_ticker_input() -> None:
        raw = st.session_state.get("ticker") or ""
        st.session_state["ticker"] = "".join(ch for ch in raw.upper() if ch.isalpha())

    # --- Sidebar: Inputs ---
    with st.sidebar:
        # Entry fields: red inner border ONLY when empty; when there's a number (or content), red disappears.
        st.markdown("""
        <style>
        /* Number inputs: red ONLY when empty (.number-empty set by JS); when has number, no red */
        [data-testid="stSidebar"] [data-testid="stNumberInput"].number-empty input,
        [data-testid="stSidebar"] [data-testid="stNumberinput"].number-empty input,
        [data-testid="stSidebar"] [data-testid="stNumberInput"].number-empty [data-baseweb],
        [data-testid="stSidebar"] [data-testid="stNumberinput"].number-empty [data-baseweb],
        [data-testid="stSidebar"] [data-testid="stNumberInput"].number-empty > div,
        [data-testid="stSidebar"] [data-testid="stNumberinput"].number-empty > div {
            border-color: #dc2626 !important;
            box-shadow: inset 0 0 0 1px #dc2626 !important;
        }
        /* Number inputs with a value: neutral border (red disappears) */
        [data-testid="stSidebar"] [data-testid="stNumberInput"]:not(.number-empty) input,
        [data-testid="stSidebar"] [data-testid="stNumberinput"]:not(.number-empty) input,
        [data-testid="stSidebar"] [data-testid="stNumberInput"]:not(.number-empty) [data-baseweb],
        [data-testid="stSidebar"] [data-testid="stNumberinput"]:not(.number-empty) [data-baseweb],
        [data-testid="stSidebar"] [data-testid="stNumberInput"]:not(.number-empty) > div,
        [data-testid="stSidebar"] [data-testid="stNumberinput"]:not(.number-empty) > div {
            border-color: #9ca3af !important;
            box-shadow: inset 0 0 0 1px #9ca3af !important;
        }
        /* Text/textarea: red when empty (placeholder shown), neutral when has content */
        [data-testid="stSidebar"] [data-testid="stTextInput"] input:placeholder-shown,
        [data-testid="stSidebar"] [data-testid="stTextinput"] input:placeholder-shown,
        [data-testid="stSidebar"] textarea:placeholder-shown {
            border-color: #dc2626 !important;
        }
        [data-testid="stSidebar"] [data-testid="stTextInput"] input:not(:placeholder-shown),
        [data-testid="stSidebar"] [data-testid="stTextinput"] input:not(:placeholder-shown),
        [data-testid="stSidebar"] textarea:not(:placeholder-shown) {
            border-color: #9ca3af !important;
        }
        /* Focus: subtle outline only, no permanent green */
        [data-testid="stSidebar"] input:focus,
        [data-testid="stSidebar"] textarea:focus {
            outline: 1px solid #9ca3af !important;
            outline-offset: 0 !important;
        }
        /* Date input: neutral (theme default); avoid forcing red */
        [data-testid="stSidebar"] div[data-testid="stDateInput"] input,
        [data-testid="stSidebar"] div[data-testid="stDateinput"] input {
            border-color: #9ca3af !important;
        }
        </style>
        """, unsafe_allow_html=True)

        # JS: add .number-empty when number input has no value (red); remove when has number (red disappears)
        _script = """
        <script>
        (function() {
            function doc() { try { return window.parent && window.parent.document ? window.parent.document : document; } catch(e) { return document; } }
            function forceTickerUppercase(inp) {
                if (!inp) return;
                var cleaned = (inp.value || '').toUpperCase().replace(/[^A-Z]/g, '');
                if (cleaned === inp.value) return;
                var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, cleaned);
                inp.dispatchEvent(new Event('input', { bubbles: true }));
            }
            function bindTickerUppercase(root) {
                var tickerInputs = root.querySelectorAll('input[aria-label="Underlying Ticker"]');
                tickerInputs.forEach(function(inp) {
                    if (inp.dataset.tickerUpperBound === '1') return;
                    inp.dataset.tickerUpperBound = '1';
                    inp.addEventListener('input', function() { forceTickerUppercase(inp); });
                    inp.addEventListener('change', function() { forceTickerUppercase(inp); });
                    forceTickerUppercase(inp);
                });
            }
            function run() {
                var root = doc();
                var sidebar = root.querySelector('[data-testid="stSidebar"]');
                if (sidebar) {
                    sidebar.querySelectorAll('[data-testid="stNumberInput"], [data-testid="stNumberinput"]').forEach(function(widget) {
                        var input = widget.querySelector('input[type="number"]');
                        if (!input) return;
                        var val = input.value;
                        var empty = val === '' || val === null || isNaN(parseFloat(val));
                        if (empty) widget.classList.add('number-empty'); else widget.classList.remove('number-empty');
                    });
                }
                bindTickerUppercase(root);
            }
            function onInput() { run(); }
            setTimeout(function() {
                run();
                var root = doc();
                var sidebar = root.querySelector('[data-testid="stSidebar"]');
                if (sidebar) {
                    sidebar.querySelectorAll('input[type="number"]').forEach(function(inp) {
                        inp.addEventListener('input', onInput);
                        inp.addEventListener('change', onInput);
                    });
                }
            }, 250);
            [500, 1000, 2000].forEach(function(ms) { setTimeout(run, ms); });
        })();
        </script>
        """
        try:
            st.html(_script, height=0, unsafe_allow_javascript=True)
        except TypeError:
            try:
                st.html(_script, unsafe_allow_javascript=True)
            except Exception:
                pass

        st.markdown("#### Profile")
        if has_supabase_config():
            st.success("Cloud storage enabled (Supabase).")
        else:
            st.warning("Cloud storage not configured. Using local file storage.")

        known_keys = list_workspace_keys()
        ensure_workspace_key(known_keys)

        if known_keys:
            options = known_keys + ["(Add new...)"]
            current_key = (st.session_state.get("workspace_key") or "").strip()
            try:
                default_index = options.index(current_key) if current_key in options else 0
            except ValueError:
                default_index = 0
            sel = st.selectbox(
                "Workspace Key (keep this private)",
                options=options,
                index=default_index,
                key="workspace_key_select",
                help="Select a workspace or add a new one. Your saved trades are grouped by workspace.",
            )
            if sel == "(Add new...)":
                new_key = st.text_input(
                    "New workspace key",
                    value="",
                    key="workspace_key_new",
                    placeholder="Type a new key and save a trade to add it to the list",
                )
                workspace_key = (new_key or "").strip() or known_keys[0]
            else:
                workspace_key = sel
            st.session_state["workspace_key"] = workspace_key
        else:
            workspace_key = st.text_input(
                "Workspace Key (keep this private)",
                value=st.session_state.get("workspace_key", ""),
                key="workspace_key",
                help="This key separates your saved trades from other users. Save a trade to create your first workspace.",
            )

        # Keep URL in sync so the key survives reruns (e.g. after Fetch Live Data).
        if workspace_key:
            try:
                q = st.query_params
                current = q.get("workspace_key") if hasattr(q, "get") else None
                if isinstance(current, list):
                    current = current[0] if current else None
                if current != workspace_key:
                    st.query_params["workspace_key"] = workspace_key
            except Exception:
                pass

        # Load saved trades data for save/update operations
        try:
            saved_trades = list_trades(workspace_key)
        except Exception as e:
            saved_trades = {}
            st.error(f"Could not load saved trades: {e}")

        # Auto-load first saved trade for this workspace *before* Schwab auto-fetch,
        # so the ticker (and other fields) are set from the loaded trade when we fetch live data.
        load_options = ["(none)"] + list(saved_trades.keys()) if saved_trades else ["(none)"]
        first_trade_label = load_options[1] if len(load_options) > 1 else None
        if (
            first_trade_label
            and st.session_state.get("last_auto_load_workspace") != workspace_key
        ):
            st.session_state["loaded_trade_data"] = saved_trades[first_trade_label]
            st.session_state["trade_to_load"] = first_trade_label
            st.session_state["last_auto_load_workspace"] = workspace_key
            st.rerun()

        st.markdown("---")

        st.markdown("#### Schwab Connection")
        if not has_schwab_config():
            st.info("Schwab API not configured. Add [schwab] secrets to enable live data.")
        else:
            if "schwab_auth_error" in st.session_state:
                st.error(f"Auth error: {st.session_state['schwab_auth_error']}")
            if "schwab_token" in st.session_state:
                # Auto-fetch live data once as soon as we connect (no button click needed)
                if st.session_state.get("auto_fetch_live_on_connect"):
                    try:
                        ticker_s = st.session_state.get("ticker", "SPY")
                        exp = st.session_state.get("expiration_date") or datetime.date.today() + datetime.timedelta(days=30)
                        short_s = st.session_state.get("short_put_strike") or 430.0
                        long_s = st.session_state.get("long_put_strike") or 420.0
                        live = fetch_schwab_live_data(ticker_s, exp, short_s, long_s)
                        live_looks_empty = (
                            (live.get("current_price") or 0) == 0
                            and (live.get("current_debit_to_close") or 0) == 0
                            and (live.get("net_delta") or 0) == 0
                            and (live.get("net_theta") or 0) == 0
                            and (live.get("net_vega") or 0) == 0
                            and (live.get("current_iv") or 0) == 0
                        )
                        if not live_looks_empty:
                            st.session_state["current_price"] = live["current_price"]
                            st.session_state["current_debit_to_close"] = live["current_debit_to_close"]
                            st.session_state["net_delta"] = live["net_delta"]
                            st.session_state["net_theta"] = live["net_theta"]
                            st.session_state["net_vega"] = live["net_vega"]
                            st.session_state["current_iv"] = live["current_iv"]
                    except Exception:
                        pass
                    st.session_state["last_live_fetch_time"] = time.time()
                    st.session_state.pop("auto_fetch_live_on_connect", None)
                    st.rerun()
                # Auto-refresh: when timer fired, fetch live data and optionally send Telegram alert
                if st.session_state.get("auto_fetch_due"):
                    try:
                        ticker_s = st.session_state.get("ticker", "SPY")
                        exp = st.session_state.get("expiration_date") or datetime.date.today() + datetime.timedelta(days=30)
                        short_s = st.session_state.get("short_put_strike") or 430.0
                        long_s = st.session_state.get("long_put_strike") or 420.0
                        live = fetch_schwab_live_data(ticker_s, exp, short_s, long_s)
                        live_looks_empty = (
                            (live.get("current_price") or 0) == 0
                            and (live.get("current_debit_to_close") or 0) == 0
                            and (live.get("net_delta") or 0) == 0
                            and (live.get("net_theta") or 0) == 0
                            and (live.get("net_vega") or 0) == 0
                            and (live.get("current_iv") or 0) == 0
                        )
                        if not live_looks_empty:
                            st.session_state["current_price"] = live["current_price"]
                            st.session_state["current_debit_to_close"] = live["current_debit_to_close"]
                            st.session_state["net_delta"] = live["net_delta"]
                            st.session_state["net_theta"] = live["net_theta"]
                            st.session_state["net_vega"] = live["net_vega"]
                            st.session_state["current_iv"] = live["current_iv"]
                            # Check recommendation and send Telegram if Close Now
                            entry_c = st.session_state.get("entry_credit") or 0.0
                            iv_entry = st.session_state.get("iv_at_entry") or 0.0
                            dte = compute_dte(exp)
                            profit_pct = (entry_c - live["current_debit_to_close"]) / entry_c * 100 if entry_c else 0.0
                            current_profit = (entry_c - live["current_debit_to_close"]) if entry_c else 0.0
                            iv_chg = compute_iv_change(live["current_iv"], iv_entry)
                            near_short = is_price_near_short_strike(live["current_price"], short_s)
                            rec, _, reasons = get_recommendation(
                                dte, profit_pct, current_profit,
                                live["net_delta"], live["net_theta"], iv_chg, near_short,
                            )
                            if rec in ("✅ Close Now", "⚠️ Close Now or Roll"):
                                trade_name = st.session_state.get("trade_label", "").strip() or f"{ticker_s} {short_s}/{long_s}"
                                send_telegram_message(
                                    f"Bull Put Spread Alert – {rec}\n"
                                    f"Trade: {trade_name}\n"
                                    f"Reasons: " + "; ".join(reasons[:3])
                                )
                    except Exception:
                        pass
                    st.session_state["last_live_fetch_time"] = time.time()
                    st.session_state.pop("auto_fetch_due", None)

                # Keep live data fresh: when connected, fetch if we haven't in the last 60 seconds (no button click)
                last_fetch = st.session_state.get("last_live_fetch_time")
                if last_fetch is None or (time.time() - last_fetch > 60):
                    try:
                        ticker_s = st.session_state.get("ticker", "SPY")
                        exp = st.session_state.get("expiration_date") or datetime.date.today() + datetime.timedelta(days=30)
                        short_s = st.session_state.get("short_put_strike") or 430.0
                        long_s = st.session_state.get("long_put_strike") or 420.0
                        live = fetch_schwab_live_data(ticker_s, exp, short_s, long_s)
                        live_looks_empty = (
                            (live.get("current_price") or 0) == 0
                            and (live.get("current_debit_to_close") or 0) == 0
                            and (live.get("net_delta") or 0) == 0
                            and (live.get("net_theta") or 0) == 0
                            and (live.get("net_vega") or 0) == 0
                            and (live.get("current_iv") or 0) == 0
                        )
                        if not live_looks_empty:
                            st.session_state["current_price"] = live["current_price"]
                            st.session_state["current_debit_to_close"] = live["current_debit_to_close"]
                            st.session_state["net_delta"] = live["net_delta"]
                            st.session_state["net_theta"] = live["net_theta"]
                            st.session_state["net_vega"] = live["net_vega"]
                            st.session_state["current_iv"] = live["current_iv"]
                        st.session_state["last_live_fetch_time"] = time.time()
                    except Exception:
                        pass

                st.success("Connected to Schwab (token stored for this session).")
                if st.button("Disconnect Schwab"):
                    st.session_state.pop("schwab_token", None)
                    try:
                        SCHWAB_TOKEN_FILE.unlink(missing_ok=True)
                    except Exception:
                        pass
                    st.rerun()
                # Export token so user can save to project folder for the background monitor (e.g. after connecting on Streamlit Cloud)
                token_data = st.session_state.get("schwab_token") or {}
                if token_data:
                    payload = dict(token_data)
                    if not payload.get("_obtained_at"):
                        payload["_obtained_at"] = datetime.datetime.now(datetime.timezone.utc).timestamp()
                    token_json = json.dumps(payload, indent=2)
                    st.download_button(
                        label="📥 Download token for monitor",
                        data=token_json,
                        file_name="schwab_token.json",
                        mime="application/json",
                        help="Save this file as schwab_token.json in your project folder so the background monitor can run on your PC.",
                    )
                if st.button("📡 Fetch Live Data"):
                    try:
                        # Capture current form values so they are not lost on rerun
                        saved_expiration = st.session_state.get("expiration_date")
                        saved_short = st.session_state.get("short_put_strike")
                        saved_long = st.session_state.get("long_put_strike")
                        saved_iv_at_entry = st.session_state.get("iv_at_entry")
                        live = fetch_schwab_live_data(
                            st.session_state["ticker"],
                            saved_expiration or datetime.date.today() + datetime.timedelta(days=30),
                            saved_short or 430.0,
                            saved_long or 420.0,
                        )
                        # Don't overwrite with zeros: if API returned no useful data, keep existing values
                        live_looks_empty = (
                            (live["current_price"] or 0) == 0
                            and (live["current_debit_to_close"] or 0) == 0
                            and (live["net_delta"] or 0) == 0
                            and (live["net_theta"] or 0) == 0
                            and (live["net_vega"] or 0) == 0
                            and (live["current_iv"] or 0) == 0
                        )
                        if live_looks_empty:
                            st.warning(
                                "Live data came back empty (all zeros). Your current values were kept. "
                                "Try again during market hours or check API response."
                            )
                        else:
                            st.session_state["current_price"] = live["current_price"]
                            st.session_state["current_debit_to_close"] = live["current_debit_to_close"]
                            st.session_state["net_delta"] = live["net_delta"]
                            st.session_state["net_theta"] = live["net_theta"]
                            st.session_state["net_vega"] = live["net_vega"]
                            st.session_state["current_iv"] = live["current_iv"]
                            st.session_state["last_live_fetch_time"] = time.time()
                            st.success("Live data loaded.")
                        # Restore form values so DTE and strikes don't reset on rerun
                        if saved_expiration is not None:
                            st.session_state["expiration_date"] = saved_expiration
                        if saved_short is not None:
                            st.session_state["short_put_strike"] = saved_short
                        if saved_long is not None:
                            st.session_state["long_put_strike"] = saved_long
                        if saved_iv_at_entry is not None:
                            st.session_state["iv_at_entry"] = saved_iv_at_entry
                            st.session_state["iv_at_entry_baseline"] = saved_iv_at_entry
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
                # Auto-refresh: continuously pull live data and alert via Telegram when Close Now
                prev_auto_refresh = st.session_state.get("auto_refresh_prev", False)
                auto_refresh = st.checkbox(
                    "Auto-refresh live data",
                    value=st.session_state.get("auto_refresh", False),
                    key="auto_refresh",
                    help="Refresh live data at the chosen interval and get Telegram alerts when recommendation is Close Now.",
                )
                st.session_state["auto_refresh_prev"] = auto_refresh
                if auto_refresh:
                    interval_min = st.selectbox(
                        "Refresh interval (minutes)",
                        options=[1, 2, 5],
                        index=0,
                        key="auto_refresh_interval",
                    )
                    if has_telegram_config():
                        st.caption("Telegram alerts enabled for Close Now / Close Now or Roll.")
                    else:
                        st.caption("Add [telegram] bot_token and chat_id in secrets for alerts. For alerts when the app is closed, run: python -m mcps.monitor")
                    if not prev_auto_refresh:
                        st.session_state["auto_fetch_due"] = True
                        st.rerun()
                else:
                    st.session_state.pop("last_live_autorefresh_tick", None)
            else:
                auth_url = build_schwab_auth_url()
                st.markdown(
                    f"<a href='{auth_url}' target='_blank' style='display:inline-block;padding:0.4rem 0.75rem;"
                    f"background-color:#2563eb;color:white;border-radius:0.35rem;text-decoration:none;font-size:0.85rem;'>"
                    f"Connect to Schwab</a>",
                    unsafe_allow_html=True,
                )

        st.markdown("---")

        # Non-blocking auto-refresh timer (no long greyed-out spinner)
        if st.session_state.get("auto_refresh") and "schwab_token" in st.session_state:
            interval_sec = int(st.session_state.get("auto_refresh_interval", 1)) * 60
            tick = st_autorefresh(interval=interval_sec * 1000, key="live_data_autorefresh_timer")
            last_tick = st.session_state.get("last_live_autorefresh_tick", -1)
            if tick != last_tick:
                st.session_state["last_live_autorefresh_tick"] = tick
                if tick > 0:
                    st.session_state["auto_fetch_due"] = True
                    st.rerun()

    # --- Main Layout ---
    st.markdown(
        "<h1 style='margin-bottom: 0.25rem;'>Bull Put Spread Analyzer – Optimal Exit Time</h1>",
        unsafe_allow_html=True,
    )
    st.caption("Quickly evaluate whether to close, hold, or roll your bull put spread based on profit, DTE, greeks, and volatility.")

    # Top panel below title: Saved Trades (left) and Manual Entry (right)
    saved_trades_col, manual_entry_col = st.columns([1.0, 1.25], gap="large")

    with saved_trades_col:
        st.markdown("#### Saved Trades")
        trade_label_value = st.text_input("Trade Name / Label", key="trade_label")
        previous_trade_label = st.session_state.get("last_trade_label_value", "")
        if trade_label_value != previous_trade_label:
            st.session_state["last_trade_label_value"] = trade_label_value
            if trade_label_value.strip() and st.session_state.get("trade_to_load", "(none)") != "(none)":
                st.session_state["trade_to_load"] = "(none)"
                st.rerun()
        load_options = ["(none)"] + list(saved_trades.keys())
        if "trade_to_load" in st.session_state and st.session_state["trade_to_load"] in load_options:
            default_load_index = load_options.index(st.session_state["trade_to_load"])
        elif saved_trades:
            default_load_index = 1
        else:
            default_load_index = 0
        trade_to_load = st.selectbox(
            "Load Trade",
            options=load_options,
            index=default_load_index,
            key="trade_to_load",
        )

        if (
            saved_trades
            and trade_to_load != "(none)"
            and trade_to_load == load_options[1]
            and st.session_state.get("last_auto_load_workspace") != workspace_key
        ):
            st.session_state["loaded_trade_data"] = saved_trades[trade_to_load]
            st.session_state["last_auto_load_workspace"] = workspace_key
            st.rerun()

        col_load_btn, col_del_btn = st.columns(2)
        with col_load_btn:
            if trade_to_load != "(none)" and st.button("📥 Apply Loaded Trade", key="apply_loaded_trade_main"):
                st.session_state["loaded_trade_data"] = saved_trades[trade_to_load]
                st.rerun()
        with col_del_btn:
            if st.button("🗑️ Delete", key="delete_trade_main"):
                if trade_to_load != "(none)":
                    try:
                        delete_trade(workspace_key, trade_to_load)
                        st.success(f"Deleted '{trade_to_load}'")
                        st.session_state["trade_to_load"] = "(none)"
                        st.rerun()
                    except Exception as e:
                        st.error(f"Delete failed: {e}")

    with manual_entry_col:
        st.markdown("#### 📝 Manual Entry")
        st.caption("Enter when you open the trade (incl. IV at entry and notes). Not updated by live fetch.")
        manual_left_col, manual_right_col = st.columns(2, gap="large")
        with manual_left_col:
            st.text_input("Underlying Ticker", key="ticker", on_change=_normalize_ticker_input)
            ticker = st.session_state.get("ticker", "SPY")

            col_strikes = st.columns(2)
            with col_strikes[0]:
                short_put_strike = st.number_input(
                    "Short Put Strike",
                    min_value=0.0,
                    value=st.session_state.get("short_put_strike", 430.0),
                    step=1.0,
                    key="short_put_strike",
                )
            with col_strikes[1]:
                long_put_strike = st.number_input(
                    "Long Put Strike",
                    min_value=0.0,
                    value=st.session_state.get("long_put_strike", 420.0),
                    step=1.0,
                    key="long_put_strike",
                )

            default_expiration = datetime.date.today() + datetime.timedelta(days=30)
            expiration_date = st.date_input(
                "Expiration Date",
                value=st.session_state.get("expiration_date", default_expiration),
                key="expiration_date",
            )

        with manual_right_col:
            entry_credit = st.number_input(
                "Entry Credit Received (per spread)",
                min_value=0.0,
                value=st.session_state.get("entry_credit", 2.00),
                step=0.05,
                format="%.2f",
                key="entry_credit",
            )

            iv_at_entry = st.number_input(
                "IV at Entry (%)",
                min_value=0.0,
                max_value=200.0,
                value=st.session_state.get("iv_at_entry", 25.0),
                step=0.5,
                format="%.2f",
                key="iv_at_entry",
            )
            st.session_state["iv_at_entry_baseline"] = iv_at_entry

            notes = st.text_area(
                "Notes / Context",
                value=st.session_state.get("notes", "e.g., broader market trend, support/resistance levels, earnings dates, etc."),
                height=120,
                key="notes",
            )

        current_trade_to_load = st.session_state.get("trade_to_load", "(none)")
        current_trade_label = (st.session_state.get("trade_label") or "").strip()
        save_target = current_trade_to_load if current_trade_to_load != "(none)" else (current_trade_label or None)
        with manual_right_col:
            save_trade_clicked = st.button("💾 Save Trade", type="primary", use_container_width=True, key="save_trade_manual")
        if save_trade_clicked:
            if save_target:
                existing = dict(saved_trades.get(save_target, {})) if save_target in saved_trades else {}
                payload = {
                    **existing,
                    "ticker": ticker,
                    "short_put_strike": short_put_strike,
                    "long_put_strike": long_put_strike,
                    "expiration_date": str(expiration_date),
                    "entry_credit": entry_credit,
                    "iv_at_entry": iv_at_entry,
                    "notes": notes,
                }
                if save_target not in saved_trades:
                    payload["current_price"] = st.session_state.get("current_price", 440.0)
                    payload["current_debit_to_close"] = st.session_state.get("current_debit_to_close", 0.40)
                    payload["net_delta"] = st.session_state.get("net_delta", 0.20)
                    payload["net_theta"] = st.session_state.get("net_theta", 3.50)
                    payload["net_vega"] = st.session_state.get("net_vega", -0.40)
                    payload["current_iv"] = st.session_state.get("current_iv", 22.0)
                try:
                    upsert_trade(workspace_key, save_target, payload)
                    st.success(f"Saved manual entry to '{save_target}' (IV at Entry {iv_at_entry}%)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")
            else:
                st.warning("Select a trade in the dropdown to save to, or enter a new trade name.")

    # Divider line under Save Trade, then fetched/calculated metrics in 2 rows (4+4)
    st.markdown(
        '<hr style="border: none; border-top: 1px solid #374151; margin: 16px 0 12px 0; opacity: 0.6;">',
        unsafe_allow_html=True,
    )
    _exp = st.session_state.get("expiration_date") or (datetime.date.today() + datetime.timedelta(days=30))
    _entry_c = st.session_state.get("entry_credit", 0.0) or 0.0
    _price = st.session_state.get("current_price", 0.0) or 0.0
    _debit = st.session_state.get("current_debit_to_close", 0.0) or 0.0
    _iv = st.session_state.get("current_iv", 0.0) or 0.0
    _iv_entry = st.session_state.get("iv_at_entry_baseline", st.session_state.get("iv_at_entry", 0.0)) or 0.0
    _delta = st.session_state.get("net_delta", 0.0) or 0.0
    _theta = st.session_state.get("net_theta", 0.0) or 0.0
    _dte = compute_dte(_exp)
    _profit, _profit_pct = compute_profit_metrics(_entry_c, _debit)
    _iv_chg = compute_iv_change(_iv, _iv_entry)

    _row1a, _row1b, _row1c, _row1d = st.columns(4)
    with _row1a:
        st.caption("Days to expiration")
        st.write(f"**{_dte} days**" if _dte >= 0 else f"**{_dte} days** (past)")
    with _row1b:
        st.caption("Underlying price")
        st.write(f"**${_price:,.2f}**")
    with _row1c:
        st.caption("Current debit to close")
        st.write(f"**${_debit:,.2f}**")
    with _row1d:
        st.caption("Potential profit")
        st.write(f"**${_profit:,.2f}** ({_profit_pct:,.1f}%)")

    _row2a, _row2b, _row2c, _row2d = st.columns(4)
    with _row2a:
        st.caption("Current IV")
        st.write(f"**{_iv:.2f}%**")
    with _row2b:
        st.caption("IV change")
        st.write(f"**{_iv_chg:+.2f}%**")
    with _row2c:
        st.caption("Net Delta")
        st.write(f"**{_delta:.2f}**")
    with _row2d:
        st.caption("Net Theta")
        st.write(f"**{_theta:+.2f}**")

    st.markdown("---")

    # Live data from Schwab (read-only; populated by Fetch Live Data / auto-fetch)
    current_price = st.session_state.get("current_price", 440.0)
    current_debit_to_close = st.session_state.get("current_debit_to_close", 0.40)
    net_delta = st.session_state.get("net_delta", 0.20)
    net_theta = st.session_state.get("net_theta", 3.50)
    net_vega = st.session_state.get("net_vega", -0.40)
    current_iv = st.session_state.get("current_iv", 22.0)
    # IV at entry: baseline for IV Change = exactly what's in Manual Entry "IV at Entry (%)"
    iv_at_entry = st.session_state.get("iv_at_entry_baseline", st.session_state.get("iv_at_entry", 25.0))

    # Derived metrics (used by recommendation and Position Snapshot)
    dte = compute_dte(expiration_date)
    current_profit, profit_pct = compute_profit_metrics(entry_credit, current_debit_to_close)
    iv_change = compute_iv_change(current_iv, iv_at_entry)
    price_near_short = is_price_near_short_strike(current_price, short_put_strike)

    st.markdown("---")

    # --- Recommendation ---
    recommendation, rec_color, rec_reasons = get_recommendation(
        dte=dte,
        profit_pct=profit_pct,
        current_profit=current_profit,
        net_delta=net_delta,
        net_theta=net_theta,
        iv_change=iv_change,
        price_near_short=price_near_short,
    )

    st.subheader("Exit Recommendation")
    render_recommendation_box(recommendation, rec_color, rec_reasons)

    # --- Detailed Summary & Reasoning ---
    col_left, col_right = st.columns([1.2, 1])

    with col_left:
        st.markdown("### Position Snapshot")
        spread_width = max(short_put_strike - long_put_strike, 0.0)
        max_profit = entry_credit
        max_loss = max(spread_width - entry_credit, 0.0)

        st.write(
            f"**{ticker} Bull Put Spread:** Short {short_put_strike:.2f} / Long {long_put_strike:.2f}, "
            f"expires {expiration_date.isoformat()}."
        )
        st.write(
            f"- **Current price**: ${current_price:,.2f}  "
            f"- **Entry credit**: ${entry_credit:,.2f}  "
            f"- **Current debit to close**: ${current_debit_to_close:,.2f}"
        )
        st.write(
            f"- **Spread width**: ${spread_width:,.2f}  "
            f"- **Max profit**: ${max_profit:,.2f}  "
            f"- **Max loss (approx.)**: ${max_loss:,.2f}"
        )
        st.write(
            f"- **Net greeks**: Δ {net_delta:.2f}, Θ {net_theta:.2f}, Vega {net_vega:.2f}  "
            f"- **IV now (live) / IV at entry**: {current_iv:.2f}% / {iv_at_entry:.2f}%"
        )
        if price_near_short:
            st.write(
                "📍 **Price is near the short strike** – assignment and gamma risk are more sensitive here."
            )
        else:
            st.write(
                "📍 **Price is away from the short strike** – more safety margin from the short leg."
            )

        if notes.strip():
            st.markdown("#### Trader Notes / Context")
            st.info(notes)

    with col_right:
        st.markdown("### Step-by-Step Reasoning")
        # Render reasoning as a bullet list in a nice box
        reasoning_md = "<div style='border-radius: 8px; padding: 0.75rem 1rem; background-color: #f9fafb; border: 1px solid #e5e7eb;'>"
        reasoning_md += "<ul style='margin: 0 0 0 1.2rem; padding-left: 0;'>"
        for r in rec_reasons:
            reasoning_md += f"<li style='margin-bottom: 0.35rem; color: #111827;'>{r}</li>"
        reasoning_md += "</ul></div>"
        st.markdown(reasoning_md, unsafe_allow_html=True)

    # --- Footer / Disclaimer ---
    st.markdown("---")
    st.caption(
        "This tool is for educational and planning purposes only and does not constitute financial advice. "
        "Always confirm with your own analysis and risk management."
    )


if __name__ == '__main__':
    main()