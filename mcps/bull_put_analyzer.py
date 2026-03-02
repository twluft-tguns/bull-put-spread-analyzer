import datetime
from typing import List, Tuple
import json
from pathlib import Path

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


def main():
    st.set_page_config(
        page_title="Bull Put Spread Analyzer – Optimal Exit Time",
        layout="wide",
        page_icon="📊",
    )

    # --- Sidebar: Inputs ---
    with st.sidebar:
        st.markdown(
            "<h2 style='margin-bottom: 0.5rem;'>Bull Put Spread Inputs</h2>",
            unsafe_allow_html=True,
        )
        st.caption("Analyze your bull put spread and get a clear exit recommendation.")

        ticker = st.text_input("Underlying Ticker", value="SPY", key="ticker")

        col_strikes = st.columns(2)
        with col_strikes[0]:
            short_put_strike = st.number_input(
                "Short Put Strike", min_value=0.0, value=430.0, step=1.0, key="short_put_strike"
            )
        with col_strikes[1]:
            long_put_strike = st.number_input(
                "Long Put Strike", min_value=0.0, value=420.0, step=1.0, key="long_put_strike"
            )

        default_expiration = datetime.date.today() + datetime.timedelta(days=30)
        expiration_date = st.date_input(
            "Expiration Date", value=default_expiration, key="expiration_date"
        )

        entry_credit = st.number_input(
            "Entry Credit Received (per spread)",
            min_value=0.0,
            value=2.00,
            step=0.05,
            format="%.2f",
            key="entry_credit",
        )

        current_price = st.number_input(
            "Current Underlying Price",
            min_value=0.0,
            value=440.0,
            step=0.5,
            format="%.2f",
            key="current_price",
        )

        current_debit_to_close = st.number_input(
            "Current Debit to Close (per spread)",
            min_value=0.0,
            value=0.40,
            step=0.05,
            format="%.2f",
            key="current_debit_to_close",
        )

        st.markdown("---")

        net_delta = st.number_input(
            "Net Delta",
            min_value=-2.0,
            max_value=2.0,
            value=0.20,
            step=0.01,
            format="%.2f",
            key="net_delta",
        )

        net_theta = st.number_input(
            "Net Theta (daily, $ per spread)",
            min_value=-20.0,
            max_value=20.0,
            value=3.50,
            step=0.10,
            format="%.2f",
            key="net_theta",
        )

        net_vega = st.number_input(
            "Net Vega",
            min_value=-10.0,
            max_value=10.0,
            value=-0.40,
            step=0.05,
            format="%.2f",
            key="net_vega",
        )

        st.markdown("---")

        current_iv = st.number_input(
            "Current IV (%)",
            min_value=0.0,
            max_value=200.0,
            value=22.0,
            step=0.5,
            format="%.2f",
            key="current_iv",
        )

        iv_at_entry = st.number_input(
            "IV at Entry (%)",
            min_value=0.0,
            max_value=200.0,
            value=25.0,
            step=0.5,
            format="%.2f",
            key="iv_at_entry",
        )

        notes = st.text_area(
            "Notes / Context",
            value="e.g., broader market trend, support/resistance levels, earnings dates, etc.",
            height=120,
            key="notes",
        )

        # --- Saved trades controls ---
        st.markdown("---")
        st.markdown("#### Saved Trades")

        saved_trades = load_saved_trades()

        trade_label = st.text_input("Trade Name / Label", key="trade_label")

        col_save, col_load = st.columns([1, 1])

        with col_save:
            if st.button("💾 Save Trade"):
                if trade_label.strip():
                    saved_trades[trade_label.strip()] = {
                        "ticker": st.session_state["ticker"],
                        "short_put_strike": st.session_state["short_put_strike"],
                        "long_put_strike": st.session_state["long_put_strike"],
                        "expiration_date": str(st.session_state["expiration_date"]),
                        "entry_credit": st.session_state["entry_credit"],
                        "current_price": st.session_state["current_price"],
                        "current_debit_to_close": st.session_state["current_debit_to_close"],
                        "net_delta": st.session_state["net_delta"],
                        "net_theta": st.session_state["net_theta"],
                        "net_vega": st.session_state["net_vega"],
                        "current_iv": st.session_state["current_iv"],
                        "iv_at_entry": st.session_state["iv_at_entry"],
                        "notes": st.session_state["notes"],
                    }
                    save_saved_trades(saved_trades)
                    st.success(f"Saved trade '{trade_label.strip()}'")

        with col_load:
            trade_to_load = st.selectbox(
                "Load Trade",
                options=["(none)"] + sorted(saved_trades.keys()),
                index=0,
                key="trade_to_load",
            )

        if trade_to_load != "(none)" and st.button("📥 Apply Loaded Trade"):
            data = saved_trades[trade_to_load]
            st.session_state["ticker"] = data["ticker"]
            st.session_state["short_put_strike"] = data["short_put_strike"]
            st.session_state["long_put_strike"] = data["long_put_strike"]
            st.session_state["expiration_date"] = datetime.date.fromisoformat(
                data["expiration_date"]
            )
            st.session_state["entry_credit"] = data["entry_credit"]
            st.session_state["current_price"] = data["current_price"]
            st.session_state["current_debit_to_close"] = data["current_debit_to_close"]
            st.session_state["net_delta"] = data["net_delta"]
            st.session_state["net_theta"] = data["net_theta"]
            st.session_state["net_vega"] = data["net_vega"]
            st.session_state["current_iv"] = data["current_iv"]
            st.session_state["iv_at_entry"] = data["iv_at_entry"]
            st.session_state["notes"] = data["notes"]
            st.experimental_rerun()

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