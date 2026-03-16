# Style-Alpha Long/Short Fund Strategy

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Status](https://img.shields.io/badge/status-research-0A7D33)
![Backtest](https://img.shields.io/badge/backtest-no--lookahead-1f6feb)
![Market](https://img.shields.io/badge/market-China%20mutual%20funds-B31B1B)

An alpha-ranking, style-balanced long/short mutual fund strategy for the China A-share fund universe.
It combines a factor-based style classification model with monthly cross-sectional alpha ranking, then trades with a no-lookahead execution rule that waits until the next weekly NAV date after each month-end signal.

This repository contains a Python rebuild of a legacy Chinese mutual fund style-classification workflow and a monthly long/short backtest built on top of it.

The strategy is:

- group funds by style
- rank funds within each style bucket by alpha
- go long the top 10% in each style bucket
- go short the bottom 10% in each style bucket
- rebalance monthly

The benchmark is:

- `80% * CSI 300 + 20% * SSE Treasury Index`

The current backtest implementation uses a no-lookahead timing rule:

- generate the signal at month-end
- trade at the next available weekly fund NAV date
- hold until the next signal's execution date

## Strategy Logic

The workflow has two layers.

### 1. Monthly style and alpha estimation

At each month-end signal date, the script rebuilds a trailing factor panel and estimates each fund's risk exposures using a three-factor regression:

`fund excess return = alpha + beta_m * (Rm - Rf) + beta_s * SMB + beta_h * HML + error`

Inputs:

- stock-level market data to build `Rm`, `SMB`, and `HML`
- mutual-fund-level NAV data to estimate each fund's alpha and style
- a risk-free-rate series from `Rf.xlsx`

By default, the backtest script uses a rolling-stability classification rule with an explicit observation schedule:

- run a full-sample regression up to the signal date
- run rolling `52`-week regressions on the same trailing history
- compute rolling-stability indicators such as `SMB_SR` and `HML_SR`
- classify funds with a three-stage rule:

  - `data_point < 52`: `Insufficient Data`
  - `52 <= data_point <= 65`: use the full-sample regression fallback
  - `data_point > 65`: use rolling-stability classification
In practice:

- if `52 <= data_point <= 65` and both `SMB` and `HML` are significant at the `5%` level, the fund is classified by the signs of `coe_smb` and `coe_hml`
- otherwise it is classified as `Alpha`
- if `data_point > 65`, the fund is classified by the signs of `SMB_SR` and `HML_SR` only when both absolute stability ratios are greater than `1`
- otherwise it is classified as `Alpha`

When you want the older, lighter classification rule, run the script with:

```bash
--classification-mode simple_significance
```

That optional mode uses a simpler two-step rule:

- `data_point < min_observations`: `Insufficient Data`
- `data_point >= min_observations`: classify by the signs of `coe_smb` and `coe_hml` only when both `p_value_smb < 0.05` and `p_value_hml < 0.05`; otherwise classify as `Alpha`

The default `min_observations` for the simple rule is `52`, and it can be changed with `--simple-min-observations`.

### 2. Portfolio construction

For each monthly signal date:

- split funds into style buckets
- rank funds within each bucket by estimated alpha
- select the top 10% and bottom 10%
- assign equal gross weight to each active style bucket
- equal-weight funds within each side of each bucket

If only two style buckets are tradable in a month, each bucket gets half of the total long gross and half of the total short gross.

## No-Lookahead Execution

The backtest is intentionally implemented to avoid the most obvious timing bias in mutual fund data.

It does **not** assume that a fund can be observed and traded at the same month-end NAV. Instead:

- the signal is built with information available through month-end
- the trade is executed at the next weekly NAV date
- the benchmark is measured over the same execution window

This matters because fund NAVs are end-of-period values, so trading at the same NAV used to form the signal would introduce look-ahead bias.

## Repository Structure

- [`style_alpha_long_short_strategy.py`](style_alpha_long_short_strategy.py)
  Main strategy and backtest script.
- [`fund_strategy_pipeline.py`](fund_strategy_pipeline.py)
  Python rebuild of the legacy factor and classification pipeline.
- [`data_input/2017Q1`](data_input/2017Q1)
  Example quarter input snapshot.
- [`akshare_index_data/sh000300.csv`](akshare_index_data/sh000300.csv)
  CSI 300 benchmark input.
- [`akshare_index_data/sh000012.csv`](akshare_index_data/sh000012.csv)
  SSE Treasury benchmark input.

## Requirements

The backtest script uses:

- Python 3.10+
- `numpy`
- `pandas`
- `matplotlib`

Example install:

```bash
pip install numpy pandas matplotlib
```

## Quick Start

Run the no-lookahead backtest on the bundled example data:

```bash
python style_alpha_long_short_strategy.py \
  --quarter-dir "data_input/2017Q1" \
  --hs300-path "akshare_index_data/sh000300.csv" \
  --treasury-path "akshare_index_data/sh000012.csv" \
  --output-dir "strategy_backtest_output/full_run_no_lookahead_prodclass"
```

Key options:

- `--history-weeks`
  Rolling lookback window used for factor regression. Default: `448`
- `--long-quantile`
  Fraction held long within each style bucket. Default: `0.10`
- `--short-quantile`
  Fraction held short within each style bucket. Default: `0.10`
- `--exclude-alpha-bucket`
  Exclude the `Alpha` bucket from portfolio construction
- `--classification-mode`
  Choose between the default `scheduled_rolling` rule and the lighter `simple_significance` rule
- `--simple-min-observations`
  Minimum history required by `simple_significance`. Default: `52`

## Outputs

The strategy script writes:

- `monthly_backtest.csv`
- `monthly_holdings.csv`
- `performance_metrics.json`
- `style_alpha_long_short_backtest.png`

The backtest output includes both signal timing and execution timing:

- `signal_date`
- `trade_date`
- `next_signal_date`
- `exit_date`

## Example Results

Using the bundled sample data and the no-lookahead execution rule, the repository currently includes two full backtest variants.

### Default: scheduled_rolling

This is the default mode in the script and uses the `<52 / 52-65 / >65` rolling-stability schedule described above.

- Strategy Ann. Return: `4.36%`
- Benchmark Ann. Return: `0.96%`
- Strategy Volatility: `5.97%`
- Benchmark Volatility: `21.80%`
- Strategy Sharpe: `0.74`
- Benchmark Sharpe: `0.15`
- Strategy Max Drawdown: `-12.33%`
- Benchmark Max Drawdown: `-35.16%`
- Monthly Hit Rate: `50.55%`
- Rebalance Months: `91`

Source:

- [`strategy_backtest_output/full_run_no_lookahead_prodclass/performance_metrics.json`](strategy_backtest_output/full_run_no_lookahead_prodclass/performance_metrics.json)

Backtest chart:

![Style-Alpha Long/Short Backtest](strategy_backtest_output/full_run_no_lookahead_prodclass/style_alpha_long_short_backtest.png)

### Alternative: simple_significance

This optional mode uses the lighter full-sample significance rule:

```bash
python style_alpha_long_short_strategy.py \
  --classification-mode simple_significance \
  --output-dir "strategy_backtest_output/full_run_no_lookahead_simple"
```

- Strategy Ann. Return: `4.48%`
- Benchmark Ann. Return: `0.96%`
- Strategy Volatility: `4.82%`
- Benchmark Volatility: `21.80%`
- Strategy Sharpe: `0.93`
- Benchmark Sharpe: `0.15`
- Strategy Max Drawdown: `-6.14%`
- Benchmark Max Drawdown: `-35.16%`
- Monthly Hit Rate: `52.75%`
- Rebalance Months: `91`

Source:

- [`strategy_backtest_output/full_run_no_lookahead_simple/performance_metrics.json`](strategy_backtest_output/full_run_no_lookahead_simple/performance_metrics.json)

Backtest chart:

![Style-Alpha Long/Short Backtest - Simple Significance](strategy_backtest_output/full_run_no_lookahead_simple/style_alpha_long_short_backtest.png)

### Why does the default rolling rule underperform the simple rule in this sample?

In this sample, the more structured rolling-stability rule does not improve returns. The annual return drops from `4.48%` to `4.36%`, and the main reason is that the short book becomes less effective after the stricter style filter reshuffles bucket membership.

- Average monthly long return: `0.98%` in `simple_significance` versus `1.11%` in `scheduled_rolling`
- Average monthly short return: `-0.60%` in `simple_significance` versus `-0.74%` in `scheduled_rolling`
- Average active style buckets: `3.70` in `simple_significance` versus `3.86` in `scheduled_rolling`
- Alpha-bucket share of holdings: `53.3%` in `simple_significance` versus `69.6%` in `scheduled_rolling`

So the default rolling rule helps the long side a bit, but it weakens the short side more than it helps the long side. Month by month the comparison is mixed, but in this run the stricter classifier also raises volatility and drawdown.

Possible optimization ideas:

- Tune the rolling-stability cutoffs instead of fixing them at `abs(SMB_SR) > 1` and `abs(HML_SR) > 1`
- Revisit the transition zone between `52` and `65` observations
- Exclude the `Alpha` bucket from portfolio construction, or cap its weight
- Use different long/short quantiles by style bucket instead of a uniform `10% / 10%`
- Add turnover controls or smoothing so style labels do not change too abruptly across months

## Important Caveats

This repository is a research and reconstruction project, not a production trading system.

Important limitations:

- The bundled backtest uses a static example fund universe from the sample quarter snapshot, so survivorship and universe-selection bias may still exist.
- Fund NAV data is weekly, so monthly rebalancing is approximated with the next available weekly NAV date.
- The monthly strategy snapshot uses a custom rolling-stability rule for backtesting: `<52` observations is treated as insufficient history, `52-65` observations uses a full-sample significance fallback, and only histories above `65` observations enter the rolling-stability classification step.
- The script also supports an optional simpler style classifier via `--classification-mode simple_significance`, but the example results shown here use the default rolling-stability schedule.
- Transaction costs, subscriptions/redemptions, liquidity constraints, and fund dealing cutoffs are not modeled.

## Related Documents

- [`STRATEGY_MODEL.md`](STRATEGY_MODEL.md)
  Higher-level explanation of the investment logic and model background.
- [`STRATEGY_README.md`](STRATEGY_README.md)
  Rebuild notes for the Python fund-style pipeline.
- [`REBALANCE_WORKFLOW.md`](REBALANCE_WORKFLOW.md)
  Operational workflow for refreshing the quarter snapshot and rerunning the classification pipeline.

## License / Use

This code is intended for research, auditing, and educational use. Validate the data, assumptions, and execution model before using it in any investment process.
