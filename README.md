# Polymart Advisor

<p align="left">
  <img src="https://polymart.co/polymartlogo.png" alt="Polymart" width="300">
</p>

A CLI portfolio management tool for the [Polymart](https://polymart.co) simulated stock exchange. Pulls live data from 132 fictional tickers across 20 sectors, runs technical analysis, builds optimized portfolios, and tracks your positions - all from your terminal.

> **All data is entirely fictional.** Polymart is a simulated market that ticks every 5 seconds. This is not financial advice.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Quick Start

### Option 1: Download the EXE (Windows)

Grab the latest release from the [Releases](../../releases) page - no Python install needed. Just download `polymart_advisor.exe` and double-click it.

### Option 2: Run from source

```bash
pip install rich requests
python polymart_advisor.py
```

Requires Python 3.8+.

---

## What It Does

| Command | Description |
|---|---|
| **Market Dashboard** | Market index, fear & greed gauge, top gainers/losers, macro snapshot, recent events |
| **Stock Screener** | Sort and filter all 132 stocks by change, price, RSI, volume, or streak - with optional sector filtering |
| **Deep Analysis** | Full technical breakdown of any ticker: SMA, EMA, MACD, Bollinger Bands, sparkline chart, multi-factor signal |
| **Sector Analysis** | Sector rotation heatmap ranked by performance, with drill-down into individual constituents |
| **Build Portfolio** | Enter a budget and risk profile, get a diversified portfolio with conviction-weighted allocation, stop-losses, and profit targets |
| **Check Portfolio** | Load your saved portfolio and compare entry prices against live data - shows P&L per position and flags triggered stops/targets |
| **Watchlist** | Persistent ticker watchlist with live signals |
| **Market Events** | Browse flash crashes, booms, FDA approvals, meme frenzies, and other simulation events |
| **Macro View** | Interest rates, inflation, GDP growth, fear & greed, and crash/boom cooldown timers |
| **Live Ticker** | Auto-refreshing price feed that updates every 6 seconds (Ctrl+C to stop) |
| **Search** | Full-text search across tickers, company names, and sectors |
| **Export CSV** | Dump all 132 stocks with prices, indicators, and computed signals to a spreadsheet |

---

## Technical Analysis

The advisor computes real indicators from Polymart's price history API:

- **SMA** (20 & 50 period) - trend direction and support/resistance
- **EMA** (12 period) - faster-reacting trend signal
- **MACD** - momentum and crossover detection
- **Bollinger Bands** (20 period, 2σ) - volatility and mean reversion
- **RSI** - overbought/oversold from the API
- **52-week range positioning** - where price sits relative to its high and low

The multi-factor scoring engine combines RSI, momentum, streak, trend bias, 52-week range, moving averages, and MACD into a single score that maps to **STRONG BUY / BUY / HOLD / SELL / STRONG SELL**.

---

## Portfolio Builder

Three risk profiles control position sizing, diversification, and risk limits:

| Profile | Positions | Max Weight | Stop Loss | Take Profit |
|---|---|---|---|---|
| Conservative | up to 10 | 15% | -5% | +10% |
| Moderate | up to 7 | 20% | -10% | +25% |
| Aggressive | up to 5 | 35% | -20% | +50% |

Allocation is conviction-weighted (higher-scoring picks get more capital) with a sector diversification cap of 2 positions per sector. Portfolios are saved to `polymart_portfolio.json` and can be checked against live prices at any time.

---

## Troubleshooting

### Windows Smart App Control / SSL errors

If the app is blocked by Smart App Control or a corporate proxy, you have a few options:

**Quick fix** - disable SSL verification for the session:
```
set POLYMART_NO_VERIFY=1
python polymart_advisor.py
```

**Proper fix** - allow Python through Smart App Control:
Settings → Privacy & Security → Windows Security → App & Browser Control → Smart App Control → Off

**Corporate proxy** - point to your CA bundle:
```
set REQUESTS_CA_BUNDLE=C:\path\to\corporate-ca-bundle.crt
```

The app will also detect SSL failures at runtime and offer to retry with verification disabled.

### Connection issues

Make sure you can reach `https://polymart.co/api/v1/getHealth` in your browser. The API requires no authentication and has no rate limit for normal usage.

---

## API

This tool uses the [Polymart REST API](https://polymart.co/#/docs/api). All endpoints are unauthenticated GET requests. Key endpoints used:

- `/api/v1/getMarket` - market index and macro overview
- `/api/v1/getStocks` - all 132 stocks with optional sector filter
- `/api/v1/getStock?ticker=X` - full detail + price history for a single stock
- `/api/v1/getSectors` - sector-level aggregates
- `/api/v1/getTopMovers` - top gainers and losers
- `/api/v1/getEvents` - simulation events (crashes, booms, etc.)
- `/api/v1/getLeaderboard` - ranked stock lists by any metric
- `/api/v1/getMacro` - macroeconomic environment
- `/api/v1/getHistory` - raw price history (up to 400 data points)
- `/api/v1/search` - full-text search

Full API docs: [polymart.co/llms.txt](https://polymart.co/#/docs/api)

---

## Files

```
polymart_advisor.py      # Source code
polymart_advisor.exe     # Standalone Windows executable (in Releases)
polymart_portfolio.json  # Your saved portfolios (created on first save)
polymart_watchlist.json  # Your watchlist (created on first save)
```

---

## License

MIT
