# Testing Retail Crypto Alpha: A Systematic Negative-Results Study of Bitcoin Microstructure Signals

*An end-to-end research project: data collection, honest backtesting, and what survives realistic costs.*

---

## Abstract

Most publicly shared crypto trading strategies claim an edge, but few survive realistic
transaction costs and honest out-of-sample testing. I built a complete research pipeline —
live data collectors, a tick-data ingestion layer, and a reusable backtesting framework — to
test, under strict pre-specified hypotheses and train/test discipline, whether a retail trader
using only public data and a single laptop can find a tradeable edge in Bitcoin market
microstructure.

I evaluated several families of hypotheses: intraday order-flow momentum and absorption,
liquidation cascades conditioned on open interest, OHLCV pattern strategies (sweeps, session
bias, gap fills), spot-versus-perpetual CVD divergence, and funding-rate mean reversion. After
modeling a realistic round-trip cost of about 0.13% and controlling for multiple testing, every
signal hypothesis was either statistically indistinguishable from noise or actively unprofitable
out-of-sample. The single apparent exception was a small-sample, regime-locked artifact that did
not survive scrutiny.

I then evaluated risk-management overlays — volatility targeting and trend filters — across eight
years of cross-asset daily data. These do not create alpha, but they reliably reduce drawdowns:
on Bitcoin they cut the maximum drawdown from about −83% to −29% while leaving the Sharpe ratio
essentially unchanged.

The central finding is honest and, I believe, useful: durable signal alpha at intraday-to-daily
horizons is not accessible to a retail trader in this setting. The achievable value lies in
disciplined risk control — exposure management and survival — not in signal discovery.

---

## 1. Introduction

Retail crypto trading is saturated with strategy claims. Order-flow "secrets," liquidation-cascade
setups, and pattern-based systems are shared widely, almost always backed by a single
good-looking backtest and almost never by honest out-of-sample evidence after costs. The gap
between "a backtest that looks profitable" and "an edge that survives live trading" is where most
retail capital is lost.

This project asks a narrow, falsifiable question: **using only public data and commodity hardware,
can a retail trader find a signal that predicts Bitcoin returns well enough to be profitable after
realistic costs?**

I approached it as a scientist, not a salesperson. A backtest is only honest if it is allowed to
return "no edge." I therefore treated "no edge" as the default hypothesis and required strong,
out-of-sample, cost-aware evidence to overturn it. Throughout, I held myself to a fixed set of
rules: hypotheses specified in advance, a strict train/test split with thresholds fit only on the
training set, a minimum sample size before drawing conclusions, bootstrap confidence intervals and
permutation tests for significance, an explicit transaction-cost model, and an awareness that
testing many ideas on one dataset inflates the chance of a false positive. When a result looked
positive on training data but reversed out-of-sample, I recorded it as overfitting — not as an
edge to be "fixed" by changing direction.

This report documents the full arc: the data infrastructure I built (Section 2), the backtesting
methodology (Section 3), each hypothesis and its result (Section 4), the pivot to risk-management
overlays once signal hypotheses were exhausted (Section 5), and the conclusions and lessons
(Section 6).

---

## 2. Data and Infrastructure

A study is only as trustworthy as the data beneath it, so the first half of this project was an
engineering effort: collecting clean data reliably, and ingesting it correctly at scale on a
single machine.

### 2.1 Live collectors

I wrote a set of collectors that subscribe to Binance WebSocket streams and persist them to dated
Parquet files: aggressive trades (the tick tape), forced liquidations, and open interest with
funding. The design goal was unattended reliability. Each stream runs as an independent client
with its own reconnect logic: on disconnection it retries with exponential backoff, and after
repeated failures it fully recreates the client rather than trusting a stale connection. This
self-healing design let the collectors run continuously without supervision.

One subtle bug was worth the effort. The streaming library occasionally returned overlapping
buffer snapshots, producing 10–17% duplicate rows. I fixed it by storing each trade's
exchange-assigned ID and rejecting any ID already seen. The live dataset used here spans
12–20 June 2026 and contains roughly 18.8 million trades, together with the matching liquidation
and open-interest series.

### 2.2 Historical data

For the longer-horizon tests I used Binance's public archives (data.binance.vision), downloading
aggregated-trade ("aggTrades") files for BTCUSDT on both the spot and USD-M perpetual markets,
covering 1 March – 19 June 2026. After ingestion these amounted to about 111 million spot trades
and 181 million perpetual trades. For the funding-rate study I pulled the full funding history and
8-hour candles through the exchange API, back to 2023.

### 2.3 Ingestion challenges

Turning raw exchange files into correct, analysis-ready bars surfaced several real-world data
problems. I solved each once and then made the solution robust:

- **Aggressor reconstruction.** Cumulative volume delta requires knowing who was the aggressor on
  each trade. I derived this from the `is_buyer_maker` flag, with care for its direction: when the
  buyer is the passive (maker) side, the aggressor is the seller.
- **Timestamp units.** Different Binance markets and periods encode time in seconds, milliseconds,
  or microseconds. A single wrong assumption collapses an entire quarter of data into one day. I
  added automatic unit detection by magnitude, which made the loader correct regardless of source.
- **Scale.** Processing 180M+ rows on a laptop required aggregating each daily file into bars on
  the fly and caching the small results, instead of holding the full tape in memory.

The result is a reusable, well-tested data layer — reliable collection, correct ingestion, and
hundreds of millions of rows handled on commodity hardware — that every experiment below could
draw on without re-solving the same problems.

## 3. Methodology: An Honest Backtesting Framework

Every result in this report came from the same framework, built around one principle: a backtest
must be allowed to say "no edge," and the burden of proof is on the signal, not on the null.

### 3.1 The default is no edge

I treated "this signal does not predict returns" as the default hypothesis, and required strong,
cost-aware, out-of-sample evidence to reject it. A good-looking in-sample result was a hypothesis
to be tested, never a conclusion.

### 3.2 Train/test discipline

I split each dataset by time, fitting any threshold or parameter only on the training portion and
evaluating once on the held-out test portion. An effect that appeared on training data but weakened
or reversed on test data was recorded as overfitting. I did not "rescue" a failed test by flipping
the signal's direction or re-tuning it — that would simply fit the noise in the test set.

### 3.3 Costs are not optional

Gross returns are irrelevant if they do not survive trading frictions. I modeled a realistic
round-trip cost of about 0.13% (taker fees plus slippage) and applied it to every position change.
The only number that counted was the net return after costs. On most intraday signals the gross
price move was 7–10× smaller than this cost — a decisive gap, not a marginal one.

### 3.4 Sample size and significance

No cell was interpreted below a minimum sample size (n ≥ 30); smaller cells were flagged and kept
out of any conclusion. For surviving cells I reported t-statistics and used bootstrap confidence
intervals and permutation tests. Where a signal was meant to act as a filter, I tested it with a
drift-aware permutation against randomly chosen bars, so that it could not be credited for returns
that were merely the market's baseline drift.

### 3.5 No look-ahead

All features were causal. Signals were shifted so that each trade used only information available
at the moment of the decision, and any "excess" move was measured against the market's
contemporaneous baseline drift rather than against zero.

### 3.6 Multiple testing

Testing many ideas on one dataset guarantees occasional false positives. I tracked how many
configurations were examined and kept in mind that roughly one spurious result with |t| > 2 is
expected for every ~20 independent tests on pure noise. A single significant cell among dozens was
therefore treated as noise unless it had been specified in advance, survived out-of-sample, and
held after a multiple-testing adjustment.

The framework is deliberately strict. Its purpose is not to find an edge but to avoid being fooled
by one that is not there — which, as the next section shows, is exactly what the data required.

## 4. Hypotheses and Results

I tested several distinct hypothesis families. Each is stated as it was specified in advance,
followed by the method, the result, and a verdict. The pattern is consistent enough to state up
front: gross effects, where they existed at all, were far too small to survive costs, and nothing
held out-of-sample.

### 4.1 Intraday order-flow: momentum and absorption

**Hypothesis.** On a one-minute tape, a strong imbalance between aggressive buying and selling
predicts the next few minutes — either as momentum (the move continues) or as absorption (a large
aggressive flow that fails to move price then reverts).

**Method.** From ~18.8M trades I built one-minute bars and cumulative volume delta, defined
aggressive-buy / aggressive-sell and buy-/sell-absorption signals, and measured net returns after
0.13% round-trip cost at horizons of 1, 3, 5, and 10 bars. I used a 70/30 train/test split and
compared every signal against the market's own baseline drift.

**Result.** The baseline drift over these horizons was a fraction of a basis point; the median
absolute move at +1 bar was about 0.024%. Every signal direction lost decisively after costs. On
the training set, aggressive-buy momentum returned roughly −0.13% to −0.15% net, with win rates of
1–16% and t-statistics from about −7 to −19; aggressive-sell was similar or worse (down to
t ≈ −24). Absorption reversals were equally negative. The test set reproduced the same picture
(aggressive-buy at −0.10% to −0.13% net, t ≈ −3 to −6); the absorption cells had too few test
signals (n < 20) to interpret and were discarded. The decisive fact is scale: the gross price move
was 7–10× smaller than the round-trip cost.

**Verdict.** No tradeable edge. These signals do not merely fail to beat costs — they are
dominated by them by nearly an order of magnitude, symmetrically on train and test.

### 4.2 Liquidation cascades conditioned on open interest

**Hypothesis.** A burst of forced liquidations clears one side of the book and is followed by a
directional move; conditioning on whether open interest rises or falls (fresh positioning versus a
squeeze / position-covering) should sharpen the signal.

**Method.** I detected cascade events in the live window (34 in total) and, for each, measured the
excess move over baseline drift and the net return after costs at 1, 5, 15, 30, and 60 minutes,
split by cascade direction and by open-interest change.

**Result.** Almost every cell was statistically indistinguishable from noise (|t| < 2) at every
horizon. One cell stood out: buy-cascades with rising open interest at +1 minute showed an excess
of +0.16% with a 100% up-rate and t = +2.76. But it rested on n = 8 — far below my n ≥ 30 gate —
did not persist at longer horizons, had a barely-positive net-of-cost return, and was confined to a
short, specific window. With more than thirty t-tests computed per run, roughly one spurious
|t| > 2.5 is expected from noise alone.

**Verdict.** No robust edge. The single suggestive cell is a small-sample, regime-locked artifact
fully consistent with multiple testing — exactly the kind of result this framework is built not to
trust. It was not pre-specified, did not survive across horizons, and would not clear realistic
slippage.

### 4.3 OHLCV pattern strategies: sweeps, session bias, gap fills

**Hypothesis.** Three popular retail setups predict short-timeframe returns: liquidity sweeps (a
stop-run beyond a prior high or low that then reverses), a session-open directional bias, and the
fill of a weekend CME gap.

**Method.** I mechanized each as a precise, testable rule and ran it on 1- and 3-minute bars across
BTC, ETH, and SOL futures, charging commissions on every entry and exit.

**Result.** Once each pattern was made fully systematic and cost-charged, none produced a positive
net expectancy. Whatever directional tendency exists in the raw pattern was smaller than the
commissions required to trade it at these timeframes.

**Verdict.** No edge after costs. These are among the most heavily marketed retail setups;
mechanized and tested honestly on short timeframes, they did not survive trading frictions.

### 4.4 Spot-versus-perpetual CVD divergence

**Hypothesis.** When the order flow of the spot and perpetual markets disagrees — one market's
cumulative volume delta pushing while the other fails to confirm — the divergence predicts the
direction in which price will resolve.

**Method.** I reconstructed hourly cumulative volume delta separately for spot (~111M trades) and
perpetual (~181M trades) over 1 March – 19 June 2026, defined a divergence signal between the two
series, and measured forward returns after costs on a train/test split.

**Result.** On the training split the signal was weakly positive but not statistically significant.
On the held-out test split it was significantly negative — the sign reversed between the two
periods. A relationship that changes sign out-of-sample is the signature of noise fitted in-sample,
not of a stable effect.

**Verdict.** No edge. The sign flip between train and test is decisive: there is no durable
relationship to trade.

### 4.5 Funding-rate z-score (mean reversion)

**Hypothesis.** Extreme funding rates signal crowded positioning, so fading the crowd — taking the
opposite side when funding is unusually high or low — should earn a mean-reversion premium.

**Method.** I pulled 3.5 years of funding history (2023–2026, ~3,800 funding intervals) with
matching 8-hour candles, and tested the fade at two z-score thresholds (1.7σ and 2.3σ), measuring
forward returns after costs on a train/test split.

**Result.** The signal was null over the full sample. Training returns were essentially flat, the
test split was weakly negative, and neither threshold produced a significant positive edge or
persisted across the two periods.

**Verdict.** No edge. Over three and a half years, fading crowded funding did not produce tradeable
returns at either threshold.

### 4.6 Absorption as a context filter (drift-aware test)

**Hypothesis.** Absorption failed as a standalone signal (Section 4.1), but it might still add value
as a context filter — flagging bars whose forward returns differ from the market as a whole.

**Method.** On both the perpetual and spot tapes, I compared returns following absorption-flagged
bars against returns following randomly chosen bars, using a drift-aware permutation test so that
any apparent effect could not simply be the market's baseline drift.

**Result.** The result was null on both venues: absorption-flagged bars were indistinguishable from
random bars once drift was removed. The two markets — independent data sources — agreed, which
strengthens the conclusion rather than leaving it to a single sample.

**Verdict.** No edge. Absorption carries no conditional information beyond baseline drift, and the
agreement across two venues makes that null robust.

### 4.7 Community patterns at scale: opening FVGs and calendar effects

**Hypothesis.** A second wave of widely shared retail patterns, tested across five assets — BTC,
ETH, SOL, gold, and oil — on 5-minute bars: (a) that a large share of "fair value gaps" formed in
the first 30 minutes after the 09:30 ET equity open stay unmitigated and set the day's direction;
(b) intraday-momentum continuation; and (c) day-of-week ("Monday") effects.

**Method.** For the opening-gap claim I measured the unmitigated rate in the opening window against
a midday control window, then traded the surviving gaps from 10:00 to the session close, net of
cost, with no look-ahead and a directional baseline. The calendar effects were measured as
day-conditioned daily returns, first on the March–June 2026 sample and then on an extended
2025–2026 sample with formal t-tests.

**Result.** The opening-gap claim did not replicate: unmitigated rates were roughly 20–36% across
all five assets — not the ~80% claimed — barely different from the control window, and every
tradable version lost after costs (net per trade from about −0.05% on gold to −0.47% on oil).
Intraday momentum was negative or zero everywhere. The day-of-week effect is the instructive case:
on the short sample, Mondays looked strongly profitable across three crypto assets at once; on the
full 2025–2026 sample the effect vanished entirely (every t-test p > 0.4).

**Verdict.** No edge — and one caution recorded for the conclusions: an apparent effect that is
consistent across assets but rests on a small sample can still be pure noise.

## 5. Pivot: Risk and Regime Overlays

Once the signal hypotheses were exhausted, the honest conclusion was that I had no reliable way to
predict direction. That changed the question. If I cannot improve *what* to trade, can I improve
*how much* to hold — turning an asset I would hold anyway into a position with a better risk
profile? This is not alpha; it is risk management, and I tested it with the same discipline.

### 5.1 The overlays

I evaluated two rule-based overlays, each with parameters fixed in advance at round values rather
than optimized:

- **Volatility targeting (VT).** Scale exposure inversely to recent realized volatility, targeting a
  constant ~20% annualized portfolio volatility: hold a fraction equal to min(1, target ÷ realized
  volatility). When the market becomes violent, exposure falls automatically.
- **Trend filter (TREND).** Hold the asset only while its price is above its 200-day moving average;
  otherwise move to cash.

I applied them to eight years of daily data across six assets — BTC, ETH, SOL, XRP, TRX, and gold
(PAXG) — benchmarked every result against simple buy-and-hold, and checked that the behaviour was
stable across a plateau of nearby parameter values rather than a single lucky setting.

### 5.2 Results

The overlays did not manufacture returns, but they reshaped risk substantially.

- **Bitcoin.** Volatility targeting plus the trend filter cut the maximum drawdown from about −83%
  to −29%, while the Sharpe ratio barely moved (≈0.66 to ≈0.69). This is almost pure de-risking:
  roughly the same risk-adjusted return, with a far shallower worst case.
- **Ethereum.** Here the overlay genuinely improved risk-adjusted return, lifting the Sharpe ratio
  from about 0.55 to 0.77 — the trend filter avoided enough of the deep declines to add efficiency,
  not merely preserve it.
- **Gold (PAXG).** Gold was the best risk-adjusted asset of the set on its own terms: a Sharpe near
  0.65 with a maximum drawdown of only about −27% — a fundamentally calmer return stream than any of
  the crypto assets.
- **Across assets.** The trend filter helped strongly trending assets (ETH, SOL) but hurt choppy
  ones (XRP, TRX), whereas volatility targeting helped more uniformly. The single best overlay
  differed from asset to asset.

### 5.3 What this does and does not show

Two honest cautions. First, that the best overlay differs per asset is itself a warning: choosing
the winning overlay for each asset after the fact would be overfitting. The defensible reading is
that volatility targeting is the more universal, lower-regret tool, and the trend filter is an
asset-specific addition suited to trending markets. Second, none of this is alpha. The overlays do
not predict returns; they change the geometry of an existing exposure — shallower drawdowns,
steadier volatility, a higher chance of staying in the game. For a trader whose binding constraint
is survival rather than forecasting, that is the more valuable lever — and, unlike every signal in
Section 4, it actually held up.

## 6. Conclusions and Lessons

### 6.1 What the data said

Across more than a dozen configurations — order flow, liquidations, open interest, OHLCV patterns,
spot–perpetual divergence, funding, opening-range gaps, momentum, and calendar effects — and across
five assets from Bitcoin to gold and oil, I found no signal that produced a tradeable edge after
realistic costs at intraday-to-daily horizons. The consistency of that null, on independent data
sources and independent markets, is itself the result. The apparent exceptions were, on inspection,
small-sample artifacts.

The reason is structural, not a matter of insufficient tuning. At short horizons the predictable
component of price is roughly an order of magnitude smaller than the round-trip cost of capturing
it. No amount of parameter search closes a gap that large; it can only appear to close it in-sample.

### 6.2 Where the value actually is

The constructive finding (Section 5) is that risk-management overlays do not predict returns but
reliably reshape them: on Bitcoin they cut the worst drawdown from about −83% to −29% at roughly
unchanged risk-adjusted return, and gold emerged as the calmest asset of the set. For a trader whose
binding constraint is survival rather than forecasting, exposure control — not signal discovery — is
the lever that actually held up out-of-sample.

### 6.3 The lesson that nearly fooled me

The most useful methodological episode was a day-of-week effect. On a short sample (sixteen Mondays,
March–June 2026) Mondays looked strongly profitable — and, more persuasively, the same pattern
appeared on Bitcoin, Ether, and Solana at once. Cross-asset agreement feels like confirmation. It
was not. Extending the sample to 2025–2026 (seventy-six Mondays) erased the effect completely, with
t-tests insignificant on every asset. The apparent consistency had been a shared market regime, not
a repeatable edge.

This is the whole discipline in one example. A short window manufactures false positives; agreement
across correlated assets does not rescue them; and the only defenses are pre-specifying the
hypothesis, demanding an adequate sample, and testing out-of-sample before believing — let alone
trading. The framework of Section 3 exists precisely so that an episode like this ends in a
discarded hypothesis rather than a deployed strategy.

### 6.4 Why a negative result is a contribution

It is tempting to read "no edge" as failure. It is the opposite. A rigorous negative result saves
capital that would otherwise be lost to costs, and time that would otherwise be spent re-testing
dead ideas. More than that: an honest, reproducible negative-results study is more credible than an
unverifiable claim of alpha. Anyone can show a profitable backtest; demonstrating the discipline to
disprove one's own ideas — repeatedly, across assets, with the statistics to back it — is the rarer
and more valuable signal. That discipline, and the infrastructure built to support it, is the real
output of this project.

---

## Appendix: Tools and Code

The project was built as a set of reusable, independently runnable Python tools. *Repository:
https://github.com/Mykola-Quant/retail-crypto-alpha*

**Data layer**
- Self-healing WebSocket collectors for trades, liquidations, and open interest / funding, with
  per-stream exponential-backoff reconnect, full client recreation after repeated failures, and
  trade-ID deduplication.
- An aggregated-trade ingestion layer with automatic timestamp-unit detection and memory-light
  per-file bar aggregation, handling 180M+ rows on a laptop.

**Backtesting harnesses**
- Order-flow momentum and absorption (train/test split, cost model, baseline drift).
- Liquidation cascades conditioned on open interest.
- Spot–perpetual CVD divergence.
- Funding-rate z-score mean reversion.
- Absorption context filter with a drift-aware permutation test.
- Opening-range FVG and calendar effects, cross-asset (BTC, ETH, SOL, gold, oil), with descriptive,
  baseline, and tradable layers plus out-of-sample weekday tests.
- Regime overlays: volatility targeting and trend filters with parameter-plateau checks and
  buy-and-hold benchmarks.

**Risk-management utilities**
- A daily target-position generator (volatility targeting + trend filter) with history export and
  charting.
- A Telegram bot exposing the daily signal and the price/position chart on demand.
