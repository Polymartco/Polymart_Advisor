#!/usr/bin/env python3
"""
Polymart Advisor — A CLI portfolio advisor for the Polymart simulated exchange.

Uses the Polymart API (https://polymart.co) to pull live simulated market data
across 132 fictional tickers and 20 sectors. Provides technical analysis,
portfolio construction, sector rotation analysis, event-driven alerts,
a live market dashboard, and persistent portfolio tracking.

All data is entirely fictional. This is not financial advice.
"""

import requests
import requests.adapters
import csv
import json
import os
import sys
import time
import math
import ssl
import signal as sig_module
import urllib3
from datetime import datetime
from collections import defaultdict

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, FloatPrompt, IntPrompt, Confirm
from rich.columns import Columns
from rich.text import Text
from rich.align import Align
from rich.live import Live
from rich.layout import Layout
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.rule import Rule
from rich import box

# ── Config ──────────────────────────────────────────────────────────────────────

console = Console()
BASE = "https://polymart.co/api/v1"
PORTFOLIO_FILE = "polymart_portfolio.json"
WATCHLIST_FILE = "polymart_watchlist.json"

RISK_PROFILES = {
    "1": {
        "name": "Conservative",
        "desc": "Low volatility, tight stops, many positions",
        "max_positions": 10,
        "max_weight": 0.15,
        "stop_loss": 0.05,
        "take_profit": 0.10,
        "rsi_buy_below": 35,
        "rsi_sell_above": 70,
        "min_streak": -2,
        "prefer_mcap": "large",
    },
    "2": {
        "name": "Moderate",
        "desc": "Balanced risk/reward, medium concentration",
        "max_positions": 7,
        "max_weight": 0.20,
        "stop_loss": 0.10,
        "take_profit": 0.25,
        "rsi_buy_below": 40,
        "rsi_sell_above": 72,
        "min_streak": -4,
        "prefer_mcap": None,
    },
    "3": {
        "name": "Aggressive",
        "desc": "High conviction, concentrated, wide stops",
        "max_positions": 5,
        "max_weight": 0.35,
        "stop_loss": 0.20,
        "take_profit": 0.50,
        "rsi_buy_below": 45,
        "rsi_sell_above": 78,
        "min_streak": -8,
        "prefer_mcap": None,
    },
}


# ── API Layer ───────────────────────────────────────────────────────────────────

# Session with retry logic. Created lazily so the SSL workaround
# only kicks in when the normal path fails.
_session = None
_ssl_workaround = False

CONNECTION_HELP = """
[yellow bold]Connection blocked — likely Windows Smart App Control or corporate SSL inspection.[/]

Try these fixes (in order):

  [bold]1.[/] Allow Python through Smart App Control / Windows Security:
     Settings → Privacy & Security → Windows Security → App & Browser Control
     → Smart App Control → set to [bold]Off[/] (or "Evaluate")

  [bold]2.[/] If on a corporate network, export your proxy CA certificate and set:
     [cyan]set REQUESTS_CA_BUNDLE=C:\\path\\to\\corporate-ca-bundle.crt[/]

  [bold]3.[/] If you trust this network, re-run with SSL verification disabled:
     [cyan]set POLYMART_NO_VERIFY=1[/]
     then restart the advisor.

  [bold]4.[/] Check that polymart.co isn't blocked by your firewall or DNS.
     Try opening [cyan]https://polymart.co/api/v1/getHealth[/] in your browser.
"""


def _get_session():
    """Build a requests.Session, optionally disabling SSL verify."""
    global _session, _ssl_workaround
    if _session is not None:
        return _session

    _session = requests.Session()

    # Retry adapter for transient failures
    adapter = requests.adapters.HTTPAdapter(
        max_retries=urllib3.util.Retry(
            total=2, backoff_factor=0.3,
            status_forcelist=[502, 503, 504],
        )
    )
    _session.mount("https://", adapter)
    _session.mount("http://", adapter)

    # If user opted in to skip verification
    if os.environ.get("POLYMART_NO_VERIFY", "").strip() in ("1", "true", "yes"):
        _session.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _ssl_workaround = True

    return _session


def api_get(endpoint, params=None, label="Fetching data"):
    """GET from the Polymart API with a spinner and robust error handling."""
    global _session, _ssl_workaround

    with Progress(
        SpinnerColumn("dots"),
        TextColumn("[cyan]{task.description}"),
        transient=True,
        console=console,
    ) as prog:
        prog.add_task(description=label, total=None)

        session = _get_session()
        url = f"{BASE}/{endpoint}"

        try:
            r = session.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()

        except (requests.exceptions.SSLError, ssl.SSLError) as e:
            # First SSL failure: offer the no-verify workaround
            if not _ssl_workaround:
                console.print(f"\n[red]✗ SSL/TLS error: {e}[/red]")
                console.print(CONNECTION_HELP)

                if Confirm.ask("Retry with SSL verification disabled for this session?", default=False):
                    _session = None  # reset session
                    os.environ["POLYMART_NO_VERIFY"] = "1"
                    return api_get(endpoint, params=params, label=label)
            else:
                console.print(f"[red]✗ SSL error (verification already disabled): {e}[/red]")
            return None

        except requests.exceptions.ConnectionError as e:
            err_str = str(e).lower()
            if "certificate" in err_str or "ssl" in err_str or "handshake" in err_str:
                console.print(f"\n[red]✗ Connection blocked (SSL/certificate issue):[/red]")
                console.print(f"  [dim]{e}[/dim]")
                console.print(CONNECTION_HELP)

                if not _ssl_workaround and Confirm.ask(
                    "Retry with SSL verification disabled for this session?", default=False
                ):
                    _session = None
                    os.environ["POLYMART_NO_VERIFY"] = "1"
                    return api_get(endpoint, params=params, label=label)
            else:
                console.print(f"[red]✗ Connection failed: {e}[/red]")
                console.print("  [dim]Check your internet connection and that polymart.co is reachable.[/dim]")
            return None

        except requests.exceptions.Timeout:
            console.print("[red]✗ Request timed out. The API may be slow or unreachable.[/red]")
            return None

        except requests.RequestException as e:
            console.print(f"[red]✗ API error: {e}[/red]")
            return None


def get_market():
    return api_get("getMarket", label="Loading market overview")


def get_stocks(sector=None):
    params = {"sector": sector} if sector else None
    return api_get("getStocks", params=params, label="Loading stocks")


def get_stock(ticker):
    return api_get("getStock", {"ticker": ticker}, label=f"Loading {ticker}")


def get_sectors():
    return api_get("getSectors", label="Loading sectors")


def get_sector(key):
    return api_get("getSector", {"sector": key}, label=f"Loading sector: {key}")


def get_events(limit=15, sector=None):
    p = {"limit": limit}
    if sector:
        p["sector"] = sector
    return api_get("getEvents", p, label="Loading events")


def get_top_movers(limit=10):
    return api_get("getTopMovers", {"limit": limit}, label="Loading top movers")


def get_leaderboard(by="change", direction="desc", limit=15):
    return api_get(
        "getLeaderboard",
        {"by": by, "dir": direction, "limit": limit},
        label=f"Ranking by {by}",
    )


def get_macro():
    return api_get("getMacro", label="Loading macro data")


def get_history(ticker, limit=200):
    return api_get("getHistory", {"ticker": ticker, "limit": limit}, label=f"Loading history for {ticker}")


def search_stocks(query):
    return api_get("search", {"q": query}, label=f'Searching "{query}"')


# ── Technical Analysis Helpers ──────────────────────────────────────────────────

def compute_sma(prices, period):
    """Simple moving average over the last `period` prices."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def compute_ema(prices, period):
    """Exponential moving average."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def compute_macd(prices):
    """MACD line, signal line, and histogram."""
    if len(prices) < 26:
        return None, None, None
    ema12 = compute_ema(prices, 12)
    ema26 = compute_ema(prices, 26)
    macd_line = ema12 - ema26
    # For signal we'd ideally want a series; approximate with recent data
    signal = macd_line * 0.7  # simplified
    histogram = macd_line - signal
    return macd_line, signal, histogram


def compute_bollinger(prices, period=20):
    """Bollinger bands: upper, middle, lower."""
    if len(prices) < period:
        return None, None, None
    window = prices[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return mean + 2 * std, mean, mean - 2 * std


def price_vs_range(price, lo52w, hi52w):
    """Where current price sits in the 52-week range as a percentage."""
    if hi52w == lo52w:
        return 50.0
    return (price - lo52w) / (hi52w - lo52w) * 100


def ascii_sparkline(prices, width=30):
    """Render a tiny ASCII chart from a price list."""
    if not prices or len(prices) < 2:
        return ""
    # Downsample to width
    step = max(1, len(prices) // width)
    sampled = [prices[i] for i in range(0, len(prices), step)][:width]
    lo, hi = min(sampled), max(sampled)
    if hi == lo:
        return "▬" * len(sampled)
    blocks = " ▁▂▃▄▅▆▇█"
    return "".join(blocks[min(8, int((p - lo) / (hi - lo) * 8))] for p in sampled)


def format_volume(v):
    if v >= 1e9:
        return f"{v/1e9:.1f}B"
    if v >= 1e6:
        return f"{v/1e6:.1f}M"
    if v >= 1e3:
        return f"{v/1e3:.0f}K"
    return str(v)


def color_change(val):
    if val > 0:
        return f"[green]+{val:.2f}%[/]"
    elif val < 0:
        return f"[red]{val:.2f}%[/]"
    return f"[dim]{val:.2f}%[/]"


def color_rsi(val):
    if val < 30:
        return f"[green]{val:.0f}[/]"
    if val > 70:
        return f"[red]{val:.0f}[/]"
    if val > 60:
        return f"[yellow]{val:.0f}[/]"
    return f"{val:.0f}"


def signal_label(sig):
    colors = {
        "STRONG BUY": "bold green",
        "BUY": "green",
        "HOLD": "yellow",
        "SELL": "red",
        "STRONG SELL": "bold red",
    }
    return f"[{colors.get(sig, 'white')}]{sig}[/]"


# ── Signal Scoring Engine ───────────────────────────────────────────────────────

def score_stock(stock, history_prices=None, risk="2"):
    """
    Multi-factor scoring. Returns (signal, score, reasons).
    Uses: RSI, momentum, streak, 52w range position, trend, volume,
    moving averages (if history available).
    """
    profile = RISK_PROFILES[risk]
    score = 0
    reasons = []

    rsi = stock.get("rsi", 50)
    change = stock.get("change", 0)
    streak = stock.get("streak", 0)
    trend = stock.get("trend", 0)
    volatility = stock.get("volatility", 0)
    price = stock.get("price", 0)
    hi52 = stock.get("hi52w", price)
    lo52 = stock.get("lo52w", price)

    # RSI
    if rsi < 30:
        score += 3
        reasons.append("RSI oversold (<30)")
    elif rsi < profile["rsi_buy_below"]:
        score += 1
        reasons.append(f"RSI low ({rsi:.0f})")
    elif rsi > 80:
        score -= 3
        reasons.append("RSI overbought (>80)")
    elif rsi > profile["rsi_sell_above"]:
        score -= 1
        reasons.append(f"RSI elevated ({rsi:.0f})")

    # Momentum / recent change
    if change > 3:
        score += 2
        reasons.append(f"Strong momentum (+{change:.1f}%)")
    elif change > 1:
        score += 1
        reasons.append("Positive momentum")
    elif change < -3:
        score -= 2
        reasons.append(f"Sharp drop ({change:.1f}%)")
    elif change < -1:
        score -= 1
        reasons.append("Negative drift")

    # Streak
    if streak >= 5:
        score += 1
        reasons.append(f"Winning streak ({streak} ticks)")
    elif streak <= -5:
        score -= 1
        reasons.append(f"Losing streak ({streak} ticks)")

    # Trend bias
    if trend > 0.005:
        score += 1
        reasons.append("Underlying uptrend")
    elif trend < -0.005:
        score -= 1
        reasons.append("Underlying downtrend")

    # 52-week range position
    range_pct = price_vs_range(price, lo52, hi52)
    if range_pct < 20:
        score += 2
        reasons.append("Near 52-week low")
    elif range_pct > 90:
        score -= 1
        reasons.append("Near 52-week high")

    # Moving average signals (if we have history)
    if history_prices and len(history_prices) >= 50:
        sma20 = compute_sma(history_prices, 20)
        sma50 = compute_sma(history_prices, 50)
        if sma20 and sma50:
            if sma20 > sma50 and price > sma20:
                score += 1
                reasons.append("Above SMA20 > SMA50 (golden alignment)")
            elif sma20 < sma50 and price < sma20:
                score -= 1
                reasons.append("Below SMA20 < SMA50 (death cross)")

        macd_line, macd_sig, _ = compute_macd(history_prices)
        if macd_line is not None:
            if macd_line > 0 and macd_line > macd_sig:
                score += 1
                reasons.append("MACD bullish")
            elif macd_line < 0 and macd_line < macd_sig:
                score -= 1
                reasons.append("MACD bearish")

    # Map score to signal
    if score >= 5:
        sig = "STRONG BUY"
    elif score >= 2:
        sig = "BUY"
    elif score <= -5:
        sig = "STRONG SELL"
    elif score <= -2:
        sig = "SELL"
    else:
        sig = "HOLD"

    return sig, score, reasons


# ── Portfolio Construction ──────────────────────────────────────────────────────

def build_portfolio(budget, stocks_data, risk="2"):
    """
    Score every stock, select the best candidates, allocate capital
    using a conviction-weighted scheme.
    """
    profile = RISK_PROFILES[risk]
    scored = []

    for ticker, stock in stocks_data.items():
        sig, sc, reasons = score_stock(stock, risk=risk)
        if sc >= 2:  # Only BUY or STRONG BUY
            scored.append(
                {
                    "ticker": ticker,
                    "name": stock.get("name", ticker),
                    "sector": stock.get("sector", "?"),
                    "mcap": stock.get("mcap", "?"),
                    "price": stock["price"],
                    "change": stock.get("change", 0),
                    "rsi": stock.get("rsi", 50),
                    "score": sc,
                    "signal": sig,
                    "reasons": reasons,
                    "volatility": stock.get("volatility", 0),
                }
            )

    # Prefer large-cap for conservative
    if profile["prefer_mcap"]:
        scored.sort(key=lambda x: (x["mcap"] != profile["prefer_mcap"], -x["score"]))
    else:
        scored.sort(key=lambda x: -x["score"])

    # Sector diversification: max 2 per sector
    sector_count = defaultdict(int)
    filtered = []
    for s in scored:
        if sector_count[s["sector"]] < 2:
            filtered.append(s)
            sector_count[s["sector"]] += 1
        if len(filtered) >= profile["max_positions"]:
            break

    if not filtered:
        return [], 0

    # Conviction-weighted allocation
    total_score = sum(s["score"] for s in filtered)
    portfolio = []
    total_cost = 0

    for item in filtered:
        raw_weight = item["score"] / total_score
        weight = min(raw_weight, profile["max_weight"])
        alloc = budget * weight
        shares = int(alloc // item["price"])
        if shares <= 0:
            continue
        cost = shares * item["price"]
        portfolio.append(
            {
                **item,
                "shares": shares,
                "cost": round(cost, 2),
                "weight": round(weight * 100, 1),
                "stop_loss": round(item["price"] * (1 - profile["stop_loss"]), 2),
                "take_profit": round(item["price"] * (1 + profile["take_profit"]), 2),
            }
        )
        total_cost += cost

    return portfolio, round(total_cost, 2)


# ── Persistence ─────────────────────────────────────────────────────────────────

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def load_json(filepath, default=None):
    if not os.path.exists(filepath):
        return default if default is not None else []
    with open(filepath) as f:
        return json.load(f)


# ── Display Helpers ─────────────────────────────────────────────────────────────

LOGO = """[cyan]
  ██████╗  ██████╗ ██╗  ██╗   ██╗███╗   ███╗ █████╗ ██████╗ ████████╗
  ██╔══██╗██╔═══██╗██║  ╚██╗ ██╔╝████╗ ████║██╔══██╗██╔══██╗╚══██╔══╝
  ██████╔╝██║   ██║██║   ╚████╔╝ ██╔████╔██║███████║██████╔╝   ██║
  ██╔═══╝ ██║   ██║██║    ╚██╔╝  ██║╚██╔╝██║██╔══██║██╔══██╗   ██║
  ██║     ╚██████╔╝███████╗██║   ██║ ╚═╝ ██║██║  ██║██║  ██║   ██║
  ╚═╝      ╚═════╝ ╚══════╝╚═╝   ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝
[/][bold]                   MARKET  ADVISOR  v2.0[/]"""


def show_header():
    console.print(LOGO)
    console.print()


def fear_greed_bar(score, label):
    """Visual fear & greed gauge."""
    width = 40
    filled = int(score / 100 * width)
    if score < 25:
        color = "red"
    elif score < 45:
        color = "yellow"
    elif score < 65:
        color = "white"
    elif score < 80:
        color = "green"
    else:
        color = "bold green"
    bar = f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"
    return f"{bar}  [{color}]{score:.0f}[/] — {label}"


# ══════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════════════════════


def cmd_dashboard():
    """Market overview dashboard — index, macro, fear/greed, movers, events."""
    market = get_market()
    if not market:
        return

    console.print()
    console.print(Rule("[bold cyan]MARKET DASHBOARD[/]"))
    console.print()

    # Index
    idx = market.get("index", 0)
    idx_chg = market.get("indexChangePct", 0)
    idx_color = "green" if idx_chg >= 0 else "red"

    index_text = Text()
    index_text.append("POLYMART INDEX  ", style="bold")
    index_text.append(f"{idx:,.2f}  ", style="bold white")
    index_text.append(
        f"{'▲' if idx_chg >= 0 else '▼'} {abs(idx_chg):.2f}%",
        style=f"bold {idx_color}",
    )
    console.print(Panel(index_text, border_style="cyan"))

    # Fear & Greed + Macro
    fg = market.get("fearGreed", 50)
    fg_label = market.get("fearGreedLabel", "Neutral")
    console.print(f"  Fear & Greed:  {fear_greed_bar(fg, fg_label)}")
    console.print(
        f"  Interest Rate: [bold]{market.get('interestRate', 0):.2f}%[/]   "
        f"Inflation: [bold]{market.get('inflation', 0):.2f}%[/]   "
        f"GDP Growth: [bold]{market.get('gdpGrowth', 0):.2f}%[/]"
    )
    console.print(
        f"  Gainers: [green]{market.get('gainers', 0)}[/]  "
        f"Losers: [red]{market.get('losers', 0)}[/]  "
        f"Unchanged: [dim]{market.get('unchanged', 0)}[/]  "
        f"Total: {market.get('totalStocks', 0)}"
    )
    console.print()

    # Top movers
    movers = get_top_movers(limit=5)
    if movers:
        t = Table(title="Top Movers", box=box.SIMPLE_HEAVY, title_style="bold")
        t.add_column("▲ Gainers", style="green")
        t.add_column("Chg%", justify="right", style="green")
        t.add_column("", width=3)
        t.add_column("▼ Losers", style="red")
        t.add_column("Chg%", justify="right", style="red")

        gainers = movers.get("gainers", [])
        losers = movers.get("losers", [])
        for i in range(max(len(gainers), len(losers))):
            g = gainers[i] if i < len(gainers) else None
            l = losers[i] if i < len(losers) else None
            t.add_row(
                f"{g['ticker']} {g.get('name', '')[:20]}" if g else "",
                f"+{g['change']:.2f}%" if g else "",
                "",
                f"{l['ticker']} {l.get('name', '')[:20]}" if l else "",
                f"{l['change']:.2f}%" if l else "",
            )
        console.print(t)

    # Recent events
    events = get_events(limit=6)
    if events:
        console.print()
        console.print("[bold]Recent Events[/]")
        for ev in events:
            eff = ev.get("effect", 0)
            icon = "[green]▲[/]" if eff > 0 else "[red]▼[/]" if eff < 0 else "[dim]●[/]"
            sector = ev.get("sector")
            fired = ev.get("firedAt", "")[:16]
            sector_display = f"[cyan]{sector}[/]" if sector else "[dim]GLOBAL[/]"
            console.print(f"  {icon} [dim]{fired}[/]  {sector_display}  {ev.get('text', '')}")


def cmd_screener():
    """Interactive stock screener with sorting and filtering."""
    console.print()
    console.print(Rule("[bold cyan]STOCK SCREENER[/]"))
    console.print()

    console.print("[dim]Sort by:[/] [1] change  [2] price  [3] rsi  [4] volume  [5] streak")
    sort_choice = Prompt.ask("Sort", choices=["1", "2", "3", "4", "5"], default="1")
    sort_map = {"1": "change", "2": "price", "3": "rsi", "4": "volume", "5": "streak"}
    sort_by = sort_map[sort_choice]

    direction = Prompt.ask("Direction", choices=["asc", "desc"], default="desc")

    sector_filter = Prompt.ask(
        "Filter by sector (enter to skip)",
        default="",
    )

    limit = IntPrompt.ask("Results", default=20)

    # Use leaderboard endpoint for sorted data
    data = get_leaderboard(by=sort_by, direction=direction, limit=min(limit, 132))
    if not data or not data.get("stocks"):
        console.print("[yellow]No results.[/]")
        return

    stocks = data["stocks"]

    # If sector filter, post-filter
    if sector_filter.strip():
        stocks = [s for s in stocks if s.get("sector", "").lower() == sector_filter.strip().lower()]

    t = Table(
        title=f"Screener — sorted by {sort_by} ({direction})",
        box=box.ROUNDED,
        show_lines=False,
        title_style="bold cyan",
    )
    t.add_column("#", style="dim", width=4)
    t.add_column("Ticker", style="bold")
    t.add_column("Name", max_width=22)
    t.add_column("Sector", style="cyan")
    t.add_column("Cap", justify="center")
    t.add_column("Price", justify="right")
    t.add_column("Chg%", justify="right")
    t.add_column("RSI", justify="right")
    t.add_column("Vol", justify="right")
    t.add_column("Streak", justify="center")

    for i, s in enumerate(stocks[:limit], 1):
        streak = s.get("streak", 0)
        streak_str = f"[green]▲{streak}[/]" if streak > 0 else f"[red]▼{abs(streak)}[/]" if streak < 0 else "—"
        t.add_row(
            str(i),
            s.get("ticker", ""),
            s.get("name", "")[:22],
            s.get("sector", ""),
            s.get("mcap", "?"),
            f"${s.get('price', 0):.2f}",
            color_change(s.get("change", 0)),
            color_rsi(s.get("rsi", 50)),
            format_volume(s.get("volume", 0)),
            streak_str,
        )

    console.print(t)
    console.print(f"  [dim]{len(stocks)} results[/]")


def cmd_analyze():
    """Deep-dive analysis of a single stock with technicals + chart."""
    console.print()
    ticker = Prompt.ask("Ticker to analyze").strip().upper()
    data = get_stock(ticker)
    if not data:
        console.print(f"[red]Could not find {ticker}.[/]")
        return

    history = data.get("history", [])
    sig, score, reasons = score_stock(data, history_prices=history)

    console.print()
    console.print(Rule(f"[bold cyan]{data['ticker']} — {data.get('name', '')}[/]"))
    console.print()

    # Price info
    price = data["price"]
    change = data.get("change", 0)
    chg_open = data.get("changeSinceOpen", 0)
    prev = data.get("previousPrice", price)

    console.print(
        f"  Price: [bold white]${price:.2f}[/]   "
        f"Tick: {color_change(change)}   "
        f"Since Open: {color_change(chg_open)}   "
        f"Prev: [dim]${prev:.2f}[/]"
    )
    console.print(
        f"  52w Range: [red]${data.get('low52w', 0):.2f}[/] — [green]${data.get('high52w', 0):.2f}[/]   "
        f"ATH: [bold]${data.get('allTimeHigh', 0):.2f}[/]   "
        f"Range Position: {price_vs_range(price, data.get('low52w', price), data.get('high52w', price)):.0f}%"
    )
    console.print(
        f"  RSI: {color_rsi(data.get('rsi', 50))}   "
        f"Momentum: {data.get('momentum', 0):.4f}   "
        f"Volatility: {data.get('volatility', 0):.4f}   "
        f"Trend: {data.get('trend', 0):.4f}"
    )
    console.print(
        f"  Streak: {data.get('streak', 0)}   "
        f"Volume: {format_volume(data.get('volume', 0))}   "
        f"Sector: [cyan]{data.get('sector', '?')}[/]   "
        f"Cap: {data.get('mcap', '?')}"
    )

    # Technical indicators from history
    if len(history) >= 20:
        sma20 = compute_sma(history, 20)
        sma50 = compute_sma(history, 50) if len(history) >= 50 else None
        ema12 = compute_ema(history, 12)
        bb_upper, bb_mid, bb_lower = compute_bollinger(history)
        macd_line, macd_sig, macd_hist = compute_macd(history)

        console.print()
        console.print("  [bold]Technical Indicators[/]")
        console.print(f"    SMA(20): ${sma20:.2f}   {'[green]price above[/]' if price > sma20 else '[red]price below[/]'}")
        if sma50:
            console.print(f"    SMA(50): ${sma50:.2f}   {'[green]price above[/]' if price > sma50 else '[red]price below[/]'}")
        if ema12:
            console.print(f"    EMA(12): ${ema12:.2f}")
        if bb_upper:
            console.print(f"    Bollinger: ${bb_lower:.2f} — ${bb_mid:.2f} — ${bb_upper:.2f}")
            if price > bb_upper:
                console.print("      [red]→ Price above upper band (overbought signal)[/]")
            elif price < bb_lower:
                console.print("      [green]→ Price below lower band (oversold signal)[/]")
        if macd_line is not None:
            console.print(f"    MACD: {macd_line:.3f}  Signal: {macd_sig:.3f}  Histogram: {macd_hist:.3f}")

    # Sparkline chart
    if history:
        console.print()
        spark = ascii_sparkline(history, width=50)
        lo, hi = min(history), max(history)
        console.print(f"  [bold]Price History[/] ({len(history)} ticks)")
        console.print(f"  ${hi:.2f} ┤")
        console.print(f"          {spark}")
        console.print(f"  ${lo:.2f} ┤")

    # Signal
    console.print()
    console.print(Panel(
        f"  Signal: {signal_label(sig)}  (score: {score:+d})\n\n"
        + "\n".join(f"    • {r}" for r in reasons),
        title="Analysis Verdict",
        border_style="cyan",
    ))

    # Sector peers
    peers = data.get("sectorPeers", [])
    if peers:
        console.print(f"\n  [dim]Sector peers: {', '.join(peers[:10])}[/]")


def cmd_sectors():
    """Sector rotation analysis — heatmap style."""
    sectors = get_sectors()
    if not sectors:
        return

    console.print()
    console.print(Rule("[bold cyan]SECTOR ANALYSIS[/]"))
    console.print()

    t = Table(box=box.ROUNDED, title_style="bold")
    t.add_column("Sector", style="bold")
    t.add_column("Icon")
    t.add_column("Avg Chg%", justify="right")
    t.add_column("Momentum", justify="right")
    t.add_column("News Impact", justify="right")
    t.add_column("Stocks", justify="right")
    t.add_column("Tickers", max_width=40)

    # Sort sectors by avgChange
    sorted_sectors = sorted(sectors.items(), key=lambda x: x[1].get("avgChange", 0), reverse=True)

    for key, sec in sorted_sectors:
        chg = sec.get("avgChange", 0)
        mom = sec.get("momentum", 0)
        news = sec.get("newsStack", 0)

        chg_color = "green" if chg > 0 else "red" if chg < 0 else "dim"
        mom_color = "green" if mom > 0 else "red" if mom < 0 else "dim"
        news_str = f"[yellow]{'▲' * min(5, max(1, int(abs(news))))}[/]" if abs(news) > 0.1 else "[dim]—[/]"

        tickers = sec.get("tickers", [])

        t.add_row(
            sec.get("label", key),
            sec.get("icon", ""),
            f"[{chg_color}]{chg:+.2f}%[/]",
            f"[{mom_color}]{mom:+.4f}[/]",
            news_str,
            str(sec.get("tickerCount", len(tickers))),
            ", ".join(tickers[:6]) + ("…" if len(tickers) > 6 else ""),
        )

    console.print(t)

    # Drill down option
    console.print()
    drill = Prompt.ask("Drill into a sector? (enter key or skip)", default="")
    if drill.strip():
        sec_data = get_sector(drill.strip())
        if sec_data and sec_data.get("stocks"):
            st = Table(
                title=f"{sec_data.get('icon', '')} {sec_data.get('label', drill)} — Constituents",
                box=box.SIMPLE,
            )
            st.add_column("Ticker", style="bold")
            st.add_column("Name")
            st.add_column("Price", justify="right")
            st.add_column("Chg%", justify="right")
            st.add_column("RSI", justify="right")
            st.add_column("Volume", justify="right")
            for s in sorted(sec_data["stocks"], key=lambda x: x.get("change", 0), reverse=True):
                st.add_row(
                    s["ticker"],
                    s.get("name", "")[:25],
                    f"${s['price']:.2f}",
                    color_change(s.get("change", 0)),
                    color_rsi(s.get("rsi", 50)),
                    format_volume(s.get("volume", 0)),
                )
            console.print(st)


def cmd_portfolio():
    """Build an optimized portfolio based on budget and risk profile."""
    console.print()
    console.print(Rule("[bold cyan]PORTFOLIO BUILDER[/]"))
    console.print()

    budget = FloatPrompt.ask("💰 Investment budget ($)", default=10000.0)
    console.print()
    for k, v in RISK_PROFILES.items():
        console.print(f"  [{k}] [bold]{v['name']}[/] — {v['desc']}")
    console.print()
    risk = Prompt.ask("Risk profile", choices=["1", "2", "3"], default="2")

    stocks = get_stocks()
    if not stocks:
        return

    portfolio, total_cost = build_portfolio(budget, stocks, risk)
    profile = RISK_PROFILES[risk]

    if not portfolio:
        console.print("[yellow]No qualifying opportunities found for this profile.[/]")
        return

    console.print()
    t = Table(
        title=f"RECOMMENDED PORTFOLIO — {profile['name'].upper()}",
        box=box.ROUNDED,
        title_style="bold cyan",
    )
    t.add_column("#", style="dim", width=3)
    t.add_column("Ticker", style="bold")
    t.add_column("Name", max_width=20)
    t.add_column("Sector", style="cyan")
    t.add_column("Signal")
    t.add_column("Score", justify="right")
    t.add_column("Shares", justify="right")
    t.add_column("Entry", justify="right")
    t.add_column("Cost", justify="right")
    t.add_column("Weight", justify="right")
    t.add_column("Stop Loss", style="red", justify="right")
    t.add_column("Target", style="green", justify="right")

    for i, p in enumerate(portfolio, 1):
        t.add_row(
            str(i),
            p["ticker"],
            p.get("name", "")[:20],
            p.get("sector", ""),
            signal_label(p["signal"]),
            f"{p['score']:+d}",
            str(p["shares"]),
            f"${p['price']:.2f}",
            f"${p['cost']:.2f}",
            f"{p['weight']:.1f}%",
            f"${p['stop_loss']:.2f}",
            f"${p['take_profit']:.2f}",
        )

    console.print(t)

    # Summary
    cash_remaining = budget - total_cost
    sectors_used = list(set(p["sector"] for p in portfolio))

    console.print()
    console.print(Panel(
        f"  Capital Deployed: [bold]${total_cost:,.2f}[/]   "
        f"Cash Reserve: [bold]${cash_remaining:,.2f}[/]   "
        f"Utilization: [bold]{total_cost/budget*100:.1f}%[/]\n"
        f"  Positions: [bold]{len(portfolio)}[/]   "
        f"Sectors: [bold]{len(sectors_used)}[/] ({', '.join(sectors_used)})\n"
        f"  Risk: [bold]{profile['name']}[/]   "
        f"Stop: [red]-{profile['stop_loss']*100:.0f}%[/]   "
        f"Target: [green]+{profile['take_profit']*100:.0f}%[/]",
        title="Portfolio Summary",
        border_style="cyan",
    ))

    # Reasons for each pick
    console.print()
    if Prompt.ask("Show reasoning for each pick?", choices=["y", "n"], default="n") == "y":
        for p in portfolio:
            console.print(f"\n  [bold]{p['ticker']}[/] — {signal_label(p['signal'])} (score {p['score']:+d})")
            for r in p.get("reasons", []):
                console.print(f"    • {r}")

    # Save
    console.print()
    if Prompt.ask("Save portfolio?", choices=["y", "n"], default="y") == "y":
        history = load_json(PORTFOLIO_FILE, [])
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "budget": budget,
            "invested": total_cost,
            "cash": round(cash_remaining, 2),
            "risk_profile": profile["name"],
            "positions": [
                {
                    "ticker": p["ticker"],
                    "name": p.get("name", ""),
                    "shares": p["shares"],
                    "entry_price": p["price"],
                    "cost": p["cost"],
                    "stop_loss": p["stop_loss"],
                    "take_profit": p["take_profit"],
                }
                for p in portfolio
            ],
        }
        history.append(entry)
        save_json(PORTFOLIO_FILE, history)
        console.print("[green]✓ Portfolio saved.[/]")


def cmd_portfolio_check():
    """Check saved portfolio against current prices."""
    history = load_json(PORTFOLIO_FILE, [])
    if not history:
        console.print("[yellow]No saved portfolios. Build one first.[/]")
        return

    last = history[-1]
    console.print()
    console.print(Rule(f"[bold cyan]PORTFOLIO CHECK — {last['timestamp']}[/]"))
    console.print(
        f"  Profile: [bold]{last['risk_profile']}[/]   "
        f"Budget: ${last['budget']:,.2f}   "
        f"Invested: ${last['invested']:,.2f}"
    )
    console.print()

    # Fetch current prices
    stocks = get_stocks()
    if not stocks:
        return

    t = Table(box=box.ROUNDED, title="Position Status", title_style="bold")
    t.add_column("Ticker", style="bold")
    t.add_column("Shares", justify="right")
    t.add_column("Entry", justify="right")
    t.add_column("Current", justify="right")
    t.add_column("P&L", justify="right")
    t.add_column("P&L %", justify="right")
    t.add_column("Stop", justify="right", style="red")
    t.add_column("Target", justify="right", style="green")
    t.add_column("Status")

    total_entry = 0
    total_current = 0

    for pos in last["positions"]:
        ticker = pos["ticker"]
        current_data = stocks.get(ticker)
        if not current_data:
            continue

        current_price = current_data["price"]
        entry_price = pos["entry_price"]
        shares = pos["shares"]
        cost = pos["cost"]
        current_val = shares * current_price
        pnl = current_val - cost
        pnl_pct = (current_price - entry_price) / entry_price * 100

        total_entry += cost
        total_current += current_val

        pnl_color = "green" if pnl >= 0 else "red"

        if current_price <= pos["stop_loss"]:
            status = "[bold red]⚠ STOP HIT[/]"
        elif current_price >= pos["take_profit"]:
            status = "[bold green]🎯 TARGET HIT[/]"
        else:
            status = "[dim]Active[/]"

        t.add_row(
            ticker,
            str(shares),
            f"${entry_price:.2f}",
            f"${current_price:.2f}",
            f"[{pnl_color}]${pnl:+,.2f}[/]",
            f"[{pnl_color}]{pnl_pct:+.2f}%[/]",
            f"${pos['stop_loss']:.2f}",
            f"${pos['take_profit']:.2f}",
            status,
        )

    console.print(t)

    total_pnl = total_current - total_entry
    total_pnl_pct = (total_pnl / total_entry * 100) if total_entry else 0
    pnl_c = "green" if total_pnl >= 0 else "red"
    console.print(
        f"\n  Total Value: [bold]${total_current:,.2f}[/]   "
        f"P&L: [{pnl_c}]${total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)[/]"
    )


def cmd_watchlist():
    """Manage a persistent watchlist."""
    watchlist = load_json(WATCHLIST_FILE, [])

    console.print()
    console.print(Rule("[bold cyan]WATCHLIST[/]"))
    console.print()
    console.print("  [1] View watchlist   [2] Add ticker   [3] Remove ticker")
    action = Prompt.ask("Action", choices=["1", "2", "3"], default="1")

    if action == "2":
        ticker = Prompt.ask("Ticker to add").strip().upper()
        if ticker and ticker not in watchlist:
            watchlist.append(ticker)
            save_json(WATCHLIST_FILE, watchlist)
            console.print(f"[green]✓ {ticker} added.[/]")
        return

    if action == "3":
        ticker = Prompt.ask("Ticker to remove").strip().upper()
        if ticker in watchlist:
            watchlist.remove(ticker)
            save_json(WATCHLIST_FILE, watchlist)
            console.print(f"[green]✓ {ticker} removed.[/]")
        return

    if not watchlist:
        console.print("[yellow]Watchlist is empty. Add some tickers first.[/]")
        return

    stocks = get_stocks()
    if not stocks:
        return

    t = Table(title="Your Watchlist", box=box.ROUNDED, title_style="bold cyan")
    t.add_column("Ticker", style="bold")
    t.add_column("Name")
    t.add_column("Price", justify="right")
    t.add_column("Chg%", justify="right")
    t.add_column("RSI", justify="right")
    t.add_column("Streak", justify="center")
    t.add_column("Signal")

    for ticker in watchlist:
        s = stocks.get(ticker)
        if not s:
            t.add_row(ticker, "[red]Not found[/]", "", "", "", "", "")
            continue
        sig, sc, _ = score_stock(s)
        streak = s.get("streak", 0)
        streak_str = f"[green]▲{streak}[/]" if streak > 0 else f"[red]▼{abs(streak)}[/]" if streak < 0 else "—"
        t.add_row(
            ticker,
            s.get("name", "")[:25],
            f"${s['price']:.2f}",
            color_change(s.get("change", 0)),
            color_rsi(s.get("rsi", 50)),
            streak_str,
            signal_label(sig),
        )

    console.print(t)


def cmd_events():
    """View market events with optional sector filter."""
    console.print()
    sector = Prompt.ask("Filter by sector (enter to skip)", default="")
    limit = IntPrompt.ask("How many events", default=15)

    events = get_events(limit=limit, sector=sector.strip() or None)
    if not events:
        console.print("[yellow]No events found.[/]")
        return

    console.print()
    console.print(Rule("[bold cyan]MARKET EVENTS[/]"))
    console.print()

    for ev in events:
        eff = ev.get("effect", 0)
        weight = ev.get("weight", 1)
        if eff > 0:
            icon = "[green]▲[/]"
            impact = f"[green]+{eff:.2f}[/]"
        elif eff < 0:
            icon = "[red]▼[/]"
            impact = f"[red]{eff:.2f}[/]"
        else:
            icon = "[dim]●[/]"
            impact = "[dim]0[/]"

        sector_tag = ev.get("sector")
        fired = ev.get("firedAt", "")[:19]
        stars = "★" * weight
        sector_display = f"[cyan]{sector_tag:>10}[/]" if sector_tag else f"[dim]{'GLOBAL':>10}[/]"

        console.print(
            f"  {icon} [dim]{fired}[/]  {sector_display}"
            f"  {impact:>12}  [yellow]{stars}[/]  {ev.get('text', '')}"
        )


def cmd_search():
    """Search for stocks by name, ticker, or sector."""
    query = Prompt.ask("Search").strip()
    if not query:
        return

    results = search_stocks(query)
    if not results or not results.get("results"):
        console.print("[yellow]No matches.[/]")
        return

    console.print()
    t = Table(title=f'Search: "{query}" — {results["count"]} results', box=box.SIMPLE)
    t.add_column("Ticker", style="bold")
    t.add_column("Name")
    t.add_column("Sector", style="cyan")
    t.add_column("Price", justify="right")
    t.add_column("Chg%", justify="right")

    for r in results["results"]:
        t.add_row(
            r["ticker"],
            r.get("name", ""),
            r.get("sector", ""),
            f"${r['price']:.2f}",
            color_change(r.get("change", 0)),
        )
    console.print(t)


def cmd_export():
    """Export full market data to CSV."""
    stocks = get_stocks()
    if not stocks:
        return

    fname = f"polymart_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Ticker", "Name", "Sector", "MCap", "Price", "Change%", "RSI", "Volume", "Streak", "Volatility", "Hi52w", "Lo52w", "Signal", "Score"])
        for ticker, s in sorted(stocks.items()):
            sig, sc, _ = score_stock(s)
            w.writerow([
                ticker, s.get("name", ""), s.get("sector", ""), s.get("mcap", ""),
                f"{s['price']:.2f}", f"{s.get('change', 0):.2f}", f"{s.get('rsi', 0):.0f}",
                s.get("volume", 0), s.get("streak", 0), f"{s.get('volatility', 0):.4f}",
                f"{s.get('hi52w', 0):.2f}", f"{s.get('lo52w', 0):.2f}",
                sig, sc,
            ])

    console.print(f"[green]✓ Exported {len(stocks)} stocks → {fname}[/]")


def cmd_live():
    """Live-updating market ticker (refreshes every 6 seconds)."""
    console.print("[dim]Live ticker — press Ctrl+C to stop[/]")
    console.print()

    try:
        while True:
            market = get_market()
            if not market:
                time.sleep(6)
                continue

            console.clear()
            idx = market.get("index", 0)
            idx_chg = market.get("indexChangePct", 0)
            fg = market.get("fearGreed", 50)
            fg_label = market.get("fearGreedLabel", "")

            idx_icon = "▲" if idx_chg >= 0 else "▼"
            idx_color = "green" if idx_chg >= 0 else "red"

            console.print(
                f"  [bold]POLYMART INDEX[/]  [bold]{idx:,.2f}[/]  "
                f"[{idx_color}]{idx_icon} {abs(idx_chg):.2f}%[/]   "
                f"F&G: {fg:.0f} ({fg_label})   "
                f"[green]▲{market.get('gainers', 0)}[/] [red]▼{market.get('losers', 0)}[/]   "
                f"[dim]{datetime.now().strftime('%H:%M:%S')}[/]"
            )

            movers = get_top_movers(5)
            if movers:
                parts = []
                for g in movers.get("gainers", [])[:3]:
                    parts.append(f"[green]{g.get('ticker','?')} +{g.get('change',0):.1f}%[/]")
                for l in movers.get("losers", [])[:3]:
                    parts.append(f"[red]{l.get('ticker','?')} {l.get('change',0):.1f}%[/]")
                if parts:
                    console.print("  " + "  │  ".join(parts))

            time.sleep(6)

    except KeyboardInterrupt:
        console.print("\n[dim]Live ticker stopped.[/]")


def cmd_macro():
    """View macroeconomic environment."""
    data = get_macro()
    if not data:
        return

    console.print()
    console.print(Rule("[bold cyan]MACRO ENVIRONMENT[/]"))
    console.print()
    console.print(f"  Interest Rate:   [bold]{data.get('interestRate', 0):.2f}%[/]")
    console.print(f"  Inflation:       [bold]{data.get('inflation', 0):.2f}%[/]")
    console.print(f"  GDP Growth:      [bold]{data.get('gdpGrowth', 0):.2f}%[/]")
    console.print(f"  Fear & Greed:    {fear_greed_bar(data.get('fearGreed', 50), data.get('fearGreedLabel', ''))}")
    console.print()

    crash_cd = data.get("crashCooldown", 0)
    boom_cd = data.get("boomCooldown", 0)
    if crash_cd > 0:
        console.print(f"  [yellow]⚠ Crash cooldown: {crash_cd} ticks remaining[/]")
    else:
        console.print(f"  [red]⚠ Crash possible (cooldown at 0)[/]")
    if boom_cd > 0:
        console.print(f"  [yellow]⚡ Boom cooldown: {boom_cd} ticks remaining[/]")
    else:
        console.print(f"  [green]⚡ Boom possible (cooldown at 0)[/]")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN MENU
# ══════════════════════════════════════════════════════════════════════════════

MENU_ITEMS = [
    ("1", "Market Dashboard", "Index, movers, events, macro snapshot", cmd_dashboard),
    ("2", "Stock Screener", "Sort & filter all 132 stocks", cmd_screener),
    ("3", "Deep Analysis", "Full technicals on a single ticker", cmd_analyze),
    ("4", "Sector Analysis", "Sector rotation heatmap + drill-down", cmd_sectors),
    ("5", "Build Portfolio", "Optimized allocation for your budget", cmd_portfolio),
    ("6", "Check Portfolio", "P&L on your saved portfolio vs live prices", cmd_portfolio_check),
    ("7", "Watchlist", "Track your favourite tickers", cmd_watchlist),
    ("8", "Market Events", "News, crashes, booms, FDA approvals", cmd_events),
    ("9", "Macro View", "Interest rates, inflation, crash/boom risk", cmd_macro),
    ("10", "Live Ticker", "Real-time price feed (Ctrl+C to stop)", cmd_live),
    ("11", "Search", "Find stocks by name or sector", cmd_search),
    ("12", "Export CSV", "Dump all stocks + signals to spreadsheet", cmd_export),
    ("0", "Exit", "", None),
]


def main():
    while True:
        console.clear()
        show_header()

        menu = Table.grid(padding=(0, 2))
        menu.add_column(style="bold cyan", width=4, justify="right")
        menu.add_column(style="bold", width=22)
        menu.add_column(style="dim")

        for key, title, desc, _ in MENU_ITEMS:
            menu.add_row(f"[{key}]", title, desc)

        console.print(Panel(menu, border_style="dim", padding=(1, 2)))

        choice = Prompt.ask(
            "[cyan]Command[/]",
            choices=[item[0] for item in MENU_ITEMS],
            default="1",
        )

        if choice == "0":
            console.print("[dim]Goodbye.[/]")
            break

        handler = next((item[3] for item in MENU_ITEMS if item[0] == choice), None)
        if handler:
            try:
                handler()
            except KeyboardInterrupt:
                pass
            except Exception as e:
                import traceback
                console.print(f"\n[red bold]Error: {e}[/red bold]")
                console.print(f"[dim]{traceback.format_exc()}[/dim]")
            console.print()
            try:
                Prompt.ask("[dim]Press Enter to continue[/]", default="")
            except (KeyboardInterrupt, EOFError):
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        console.print(f"\n[red bold]Fatal error:[/red bold]")
        console.print(traceback.format_exc())
        input("\nPress Enter to exit...")
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/dim]")
