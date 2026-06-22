# Retail Crypto Alpha: A Negative-Results Study

**A systematic test of whether retail-accessible microstructure signals can beat costs on liquid
crypto — and an honest account of what does and does not survive.**

This repository contains the full research pipeline behind the study: live data collectors, a
tick-data ingestion layer, a reusable backtesting framework, and a suite of risk-management tools.
Every hypothesis was pre-specified, tested out-of-sample, charged realistic transaction costs, and
checked for multiple testing. The headline result is a clean negative — and a clear answer about
where achievable value actually lies.

📄 **Full write-up:** [`research_report.md`](research_report.md) · [`research_report.pdf`](research_report.pdf)

---

## TL;DR — what the data said

- Tested **more than a dozen configurations** across order flow, liquidations + open interest,
  OHLCV patterns, spot–perpetual CVD divergence, funding-rate mean reversion, opening-range gaps,
  intraday momentum, and calendar effects — on **five assets** (BTC, ETH, SOL, gold, oil).
- After a realistic **~0.13% round-trip cost** and multiple-testing controls, **no signal produced
  a tradeable edge** at intraday-to-daily horizons. At short horizons the predictable move is
  roughly an order of magnitude smaller than the cost of capturing it.
- The single apparent exceptions were small-sample artifacts. One worked example: a day-of-week
  effect that looked strong on 16 samples across three assets at once — and **vanished completely**
  when the sample was extended (76 samples, all t-tests insignificant).
- **Risk overlays do not create alpha but reshape it:** on Bitcoin, volatility targeting + a trend
  filter cut the worst drawdown from **≈ −83% to ≈ −29%** at roughly unchanged risk-adjusted return.
  Gold was the calmest asset of the set.

## Methodology

The backtesting framework is built on one rule: a backtest must be allowed to return "no edge."

- **Default = no edge.** The burden of proof is on the signal.
- **Train/test split**, with thresholds and parameters fit on the training set only.
- **Realistic cost model** (~0.13% round-trip) applied to every position change.
- **Minimum-sample gate** (n ≥ 30); bootstrap confidence intervals; permutation and drift-aware
  significance tests.
- **No look-ahead:** causal features, shifted signals, excess returns measured against baseline
  drift.
- **Multiple-testing discipline:** a single significant cell among many is treated as noise unless
  pre-specified and confirmed out-of-sample.

## Repository structure

**Data layer**
- `collectors/` — self-healing WebSocket collectors (trades, liquidations, open interest / funding)
  with exponential-backoff reconnect, client recreation, and trade-ID deduplication.
- `ingest/` — aggregated-trade ingestion with automatic timestamp-unit detection and memory-light
  per-file bar aggregation (handles 180M+ rows on a laptop).

**Backtesting harnesses**
- `honest_backtest.py` — order-flow momentum and absorption.
- `cascade_study.py` — liquidation cascades conditioned on open interest.
- `cvd_divergence.py` — spot–perpetual CVD divergence.
- `funding_zscore_backtest.py` — funding-rate z-score mean reversion.
- `absorption_backtest.py` — absorption context filter with a drift-aware permutation test.
- `opening_fvg_backtest.py`, `multi_fvg_backtest.py` — opening-range FVG and calendar effects,
  cross-asset, with descriptive / baseline / tradable layers.
- `regime_overlay.py` — volatility targeting and trend filters with parameter-plateau checks.

**Risk-management utilities**
- `daily_risk_signal.py` — daily target-position generator (vol targeting + trend filter), with
  history export and charting.
- `risk_bot.py` — Telegram bot exposing the daily signal and the price/position chart on demand.

## Getting started

```bash
python -m venv venv && source venv/bin/activate
pip install pandas numpy ccxt pyarrow matplotlib scipy

# Example: today's risk-managed target position for BTC
python daily_risk_signal.py --file btc_daily.csv

# Example: reproduce the order-flow result
python honest_backtest.py
```

Each script is independently runnable and prints its own train/test results with costs and
significance tests.

## Why a negative result

Anyone can show a profitable backtest. Demonstrating the discipline to disprove one's own ideas —
repeatedly, across assets, with the statistics to back it — is the rarer and more useful signal,
and it is what this project is really about: rigorous methodology, reliable data infrastructure, and
honest interpretation.

---

*Author: Mykola · github.com/Mykola-Quant · Built with Python on commodity hardware.*
