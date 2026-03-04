import base64
import datetime
import uuid
from typing import List, Tuple
import json
from pathlib import Path
from urllib.parse import urlencode

import requests
import streamlit as st

TRADES_FILE = Path("saved_trades.json")


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


def get_schwab_access_token() -> str:
    """Return current access_token; refresh if expired."""
    if "schwab_token" not in st.session_state:
        raise RuntimeError("Not connected to Schwab. Click 'Connect to Schwab' first.")
    token = st.session_state["schwab_token"]
    access = token.get("access_token")
    if not access:
        raise RuntimeError("Schwab token missing access_token. Reconnect to Schwab.")
    # Optional: check expires_in and refresh using refresh_token if needed
    return access


def fetch_schwab_live_data(
    ticker: str,
    expiration_date: datetime.date,
    short_put_strike: float,
    long_put_strike: float,
) -> dict:
    """
    Call Schwab marketdata chains API and return dict with:
    current_price, current_debit_to_close, net_delta, net_theta, net_vega, current_iv
    """
    access_token = get_schwab_access_token()
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
        "Authorization": f"Bearer {access_token}",
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

    # IV: QuoteOption.volatility (often decimal 0.22 = 22%)
    short_iv = greek(short_c, "volatility")
    if 0 < short_iv < 2:
        current_iv = short_iv * 100
    else:
        current_iv = short_iv

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
            except Exception as e:
                st.session_state["schwab_auth_error"] = str(e)

    # Apply any pending loaded trade data BEFORE widgets are created
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
        st.session_state["current_price"] = float(data.get("current_price") or 0.0)
        st.session_state["current_debit_to_close"] = float(data.get("current_debit_to_close") or 0.0)
        st.session_state["net_delta"] = float(data.get("net_delta") or 0.0)
        st.session_state["net_theta"] = float(data.get("net_theta") or 0.0)
        st.session_state["net_vega"] = float(data.get("net_vega") or 0.0)
        st.session_state["current_iv"] = float(data.get("current_iv") or 0.0)
        st.session_state["iv_at_entry"] = float(data.get("iv_at_entry") or 0.0)
        st.session_state["notes"] = data.get("notes") or ""

    # --- Sidebar: Inputs ---
    with st.sidebar:
        st.markdown(
            "<h2 style='margin-bottom: 0.5rem;'>Bull Put Spread Inputs</h2>",
            unsafe_allow_html=True,
        )
        st.caption("Analyze your bull put spread and get a clear exit recommendation.")

        st.markdown("#### Schwab Connection")
        if not has_schwab_config():
            st.info("Schwab API not configured. Add [schwab] secrets to enable live data.")
        else:
            if "schwab_auth_error" in st.session_state:
                st.error(f"Auth error: {st.session_state['schwab_auth_error']}")
            if "schwab_token" in st.session_state:
                st.success("Connected to Schwab (token stored for this session).")
                if st.button("Disconnect Schwab"):
                    st.session_state.pop("schwab_token", None)
                    st.rerun()
                if st.button("📡 Fetch Live Data"):
                    try:
                        # Capture current form values so they are not lost on rerun
                        saved_expiration = st.session_state.get("expiration_date")
                        saved_short = st.session_state.get("short_put_strike")
                        saved_long = st.session_state.get("long_put_strike")
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
                            st.success("Live data loaded.")
                        # Restore form values so DTE and strikes don't reset on rerun
                        if saved_expiration is not None:
                            st.session_state["expiration_date"] = saved_expiration
                        if saved_short is not None:
                            st.session_state["short_put_strike"] = saved_short
                        if saved_long is not None:
                            st.session_state["long_put_strike"] = saved_long
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
            else:
                auth_url = build_schwab_auth_url()
                st.markdown(
                    f"<a href='{auth_url}' target='_blank' style='display:inline-block;padding:0.4rem 0.75rem;"
                    f"background-color:#2563eb;color:white;border-radius:0.35rem;text-decoration:none;font-size:0.85rem;'>"
                    f"Connect to Schwab</a>",
                    unsafe_allow_html=True,
                )

        st.markdown("#### Trade Storage")
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

        st.markdown("---")
        st.markdown("#### 📝 Manual entry")
        st.caption("Enter when you open the trade (incl. IV at entry and notes). Not updated by Fetch Live Data.")
        ticker = st.text_input("Underlying Ticker", value=st.session_state.get("ticker", "SPY"), key="ticker")

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

        notes = st.text_area(
            "Notes / Context",
            value=st.session_state.get("notes", "e.g., broader market trend, support/resistance levels, earnings dates, etc."),
            height=120,
            key="notes",
        )

        st.markdown("---")
        st.markdown("#### 📡 Live / current data")
        st.caption("Filled by **Fetch Live Data** when connected to Schwab, or enter manually.")
        current_price = st.number_input(
            "Current Underlying Price",
            min_value=0.0,
            value=st.session_state.get("current_price", 440.0),
            step=0.5,
            format="%.2f",
            key="current_price",
        )

        current_debit_to_close = st.number_input(
            "Current Debit to Close (per spread)",
            min_value=0.0,
            value=st.session_state.get("current_debit_to_close", 0.40),
            step=0.05,
            format="%.2f",
            key="current_debit_to_close",
        )

        net_delta = st.number_input(
            "Net Delta",
            min_value=-2.0,
            max_value=2.0,
            value=st.session_state.get("net_delta", 0.20),
            step=0.01,
            format="%.2f",
            key="net_delta",
        )

        net_theta = st.number_input(
            "Net Theta (daily, $ per spread)",
            min_value=-20.0,
            max_value=20.0,
            value=st.session_state.get("net_theta", 3.50),
            step=0.10,
            format="%.2f",
            key="net_theta",
        )

        net_vega = st.number_input(
            "Net Vega",
            min_value=-10.0,
            max_value=10.0,
            value=st.session_state.get("net_vega", -0.40),
            step=0.05,
            format="%.2f",
            key="net_vega",
        )

        current_iv = st.number_input(
            "Current IV (%)",
            min_value=0.0,
            max_value=200.0,
            value=st.session_state.get("current_iv", 22.0),
            step=0.5,
            format="%.2f",
            key="current_iv",
        )

        # --- Saved trades controls ---
        st.markdown("---")
        st.markdown("#### Saved Trades")

        try:
            saved_trades = list_trades(workspace_key)
        except Exception as e:
            saved_trades = {}
            st.error(f"Could not load saved trades: {e}")

        trade_label = st.text_input("Trade Name / Label", key="trade_label")

        col_save, col_load, col_del = st.columns([1, 1, 1])

        with col_save:
            if st.button("💾 Save Trade"):
                if trade_label.strip():
                    # Use widget return values so we save exactly what is displayed (avoids session_state timing)
                    payload = {
                        "ticker": ticker,
                        "short_put_strike": short_put_strike,
                        "long_put_strike": long_put_strike,
                        "expiration_date": str(expiration_date),
                        "entry_credit": entry_credit,
                        "current_price": current_price,
                        "current_debit_to_close": current_debit_to_close,
                        "net_delta": net_delta,
                        "net_theta": net_theta,
                        "net_vega": net_vega,
                        "current_iv": current_iv,
                        "iv_at_entry": iv_at_entry,
                        "notes": notes,
                    }
                    try:
                        upsert_trade(workspace_key, trade_label.strip(), payload)
                        st.success(f"Saved trade '{trade_label.strip()}'")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")

        with col_load:
            # Order: most recent first (Supabase order preserved); default to first trade on load
            load_options = ["(none)"] + list(saved_trades.keys())
            if "trade_to_load" in st.session_state and st.session_state["trade_to_load"] in load_options:
                default_load_index = load_options.index(st.session_state["trade_to_load"])
            elif saved_trades:
                default_load_index = 1  # first trade
            else:
                default_load_index = 0
            trade_to_load = st.selectbox(
                "Load Trade",
                options=load_options,
                index=default_load_index,
                key="trade_to_load",
            )

            # Auto-populate manual entry with top trade when it's selected by default (e.g. on login)
            if (
                saved_trades
                and trade_to_load != "(none)"
                and trade_to_load == load_options[1]
                and st.session_state.get("last_auto_load_workspace") != workspace_key
            ):
                st.session_state["loaded_trade_data"] = saved_trades[trade_to_load]
                st.session_state["last_auto_load_workspace"] = workspace_key
                st.rerun()

        with col_del:
            if st.button("🗑️ Delete"):
                if trade_to_load != "(none)":
                    try:
                        delete_trade(workspace_key, trade_to_load)
                        st.success(f"Deleted '{trade_to_load}'")
                        st.session_state["trade_to_load"] = "(none)"
                        st.rerun()
                    except Exception as e:
                        st.error(f"Delete failed: {e}")

        if trade_to_load != "(none)" and st.button("📥 Apply Loaded Trade"):
            st.session_state["loaded_trade_data"] = saved_trades[trade_to_load]
            st.rerun()

    # --- Main Layout ---
    st.markdown(
        "<h1 style='margin-bottom: 0.25rem;'>Bull Put Spread Analyzer – Optimal Exit Time</h1>",
        unsafe_allow_html=True,
    )
    st.caption("Quickly evaluate whether to close, hold, or roll your bull put spread based on profit, DTE, greeks, and volatility.")

    # Derived metrics
    dte = compute_dte(expiration_date)
    current_profit, profit_pct = compute_profit_metrics(entry_credit, current_debit_to_close)
    iv_change = compute_iv_change(current_iv, iv_at_entry)
    price_near_short = is_price_near_short_strike(current_price, short_put_strike)

    # --- Top Metrics Row ---
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            label="Days to Expiration (DTE)",
            value=f"{dte} days" if dte >= 0 else f"{dte} days (past)",
        )
    with col2:
        st.metric(
            label="Current Profit ($ per spread)",
            value=f"${current_profit:,.2f}",
            delta=f"{profit_pct:,.1f}%",
        )
    with col3:
        st.metric(
            label="IV Change",
            value=f"{current_iv:.1f}%",
            delta=f"{iv_change:+.1f}%",
        )
    with col4:
        st.metric(
            label="Net Delta / Theta",
            value=f"{net_delta:.2f}",
            delta=f"Theta {net_theta:+.2f}",
        )

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
            f"- **IV now / entry**: {current_iv:.1f}% / {iv_at_entry:.1f}%"
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