# Daily Style-Alpha Long/Short Fund Strategy (2026)

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Status](https://img.shields.io/badge/status-research-0A7D33)
![Backtest](https://img.shields.io/badge/backtest-no--lookahead-1f6feb)
![Market](https://img.shields.io/badge/market-China%20mutual%20funds-B31B1B)

An alpha-ranking, style-balanced long/short strategy for the China active-equity mutual fund universe. It combines a factor-based style classification model with monthly cross-sectional alpha ranking, then trades with a no-lookahead execution rule that waits until the next available daily NAV date after each month-end signal.

The strategy uses:

- daily cumulative NAV (`累计净值`)
- daily CH-3 factors (`mktrf`, `SMB`, `VMG`)
- monthly no-lookahead rebalancing
- a speed-up implementation for rolling style classification and backtesting

The strategy is:

- classify funds into style buckets
- rank funds within each bucket by estimated alpha
- go long the top quantile in each active bucket
- go short the bottom quantile in each active bucket
- rebalance monthly

The benchmark is:

- `80% * CSI 300 + 20% * SSE Treasury Index`
- implemented as a daily-return series and chain-linked over each holding period

Path note:

- The daily workflow files live under `strategy_2026/`.
- The commands below assume your working directory is `strategy_2026`.
- The earlier weekly version of the strategy is in `strategy_2017/`.

## Strategy Logic

The workflow has two layers.

### 1. Daily factor panel and style estimation

For each monthly signal date, the script uses a trailing daily panel to estimate each fund's alpha and style exposure:

`fund excess return = alpha + beta_m * mktrf + beta_s * SMB + beta_v * VMG + error`

The default classification rule is a three-stage schedule:

- `data_point < 245`: `Insufficient Data`
- `245 <= data_point <= 320`: use the full-sample regression fallback
- `data_point > 320`: use rolling-stability classification

Under the current reference setup:

- `ROLLING_WINDOW = 245`
- `P_VALUE_THRESHOLD = 0.05`
- `SMB_STABILITY_THRESHOLD = 1.5`
- `VMG_STABILITY_THRESHOLD = 1.5`

In practice:

- if `245 <= data_point <= 320` and both `SMB` and `VMG` are significant at the `5%` level, the fund is classified by the signs of `coe_smb` and `coe_vmg`
- otherwise it is classified as `Alpha`
- if `data_point > 320`, the fund is classified by the signs of `SMB_SR` and `VMG_SR` only when both absolute stability ratios are greater than `1.5`
- otherwise it is classified as `Alpha`

### 2. Monthly portfolio construction

For each month-end signal date:

- build the eligible fund universe
- split funds into style buckets:
  `Large Growth`, `Large Value`, `Small Growth`, `Small Value`, and optionally `Alpha`
- rank funds within each bucket by estimated alpha
- long the top quantile and short the bottom quantile
- trade at the next available NAV date
- hold until the next execution date, with the last portfolio marked to `BACKTEST_END_DATE`

## No-Lookahead Execution

The backtest is intentionally implemented to avoid obvious timing bias in mutual fund data.

It does **not** assume that a fund can be observed and traded at the same NAV used to form the signal. Instead:

- the signal is built with information available through month-end
- the portfolio is traded at the next available daily NAV date
- the benchmark is measured over the same realized holding window

## Data Inputs

The current daily strategy uses:

- [fund_info_all.csv](data/fund_info_all.csv)
  fund metadata
- [nav_cum.csv](data/nav_cum.csv)
  daily cumulative NAV wide table
- [stock_holding_wide_2000_2025.csv](data/stock_holding_wide_2000_2025.csv)
  quarterly stock-holding filter input
- [CH3_factors_daily_202602.xlsx](data/CH3_factors_daily_202602.xlsx)
  daily CH-3 factor file
- [sh000300.csv](data/benchmark/sh000300.csv)
  CSI 300 benchmark input
- [sh000012.csv](data/benchmark/sh000012.csv)
  SSE Treasury benchmark input

The current implementation also removes duplicate `A/C/E` share classes before backtesting and keeps one representative share class per underlying fund when possible.

## Repository Structure

- [strategy_2026.py](strategy_2026.py)
  main daily strategy and backtest script
- [strategy_2026_parameter_search.py](strategy_2026_parameter_search.py)
  parameter-search driver built on top of the speed-up script
- [WORKFLOW.md](WORKFLOW.md)
  detailed research notes, data review, and regime discussion
- [MINGSHI_CH3_EXPLANATION.md](MINGSHI_CH3_EXPLANATION.md)
  notes on the CH-3 factor construction and interpretation

## Requirements

The daily strategy uses:

- Python 3.10+
- `numpy`
- `pandas`
- `matplotlib`
- `scipy`
- `statsmodels`
- `openpyxl`

Example install:

```bash
pip install numpy pandas matplotlib scipy statsmodels openpyxl
```

## Quick Start

Run the current reference daily backtest:

```bash
python strategy_2026.py
```

The main script is configured through top-of-file parameters, including:

- `BACKTEST_START_DATE`
- `BACKTEST_END_DATE`
- `BACKTEST_MIN_STOCK_HOLDING`
- `BACKTEST_LONG_QUANTILE`
- `BACKTEST_SHORT_QUANTILE`
- `BACKTEST_INCLUDE_ALPHA_BUCKET`
- `ROLLING_WINDOW`
- `FULL_SAMPLE_FALLBACK_MAX`
- `P_VALUE_THRESHOLD`
- `SMB_STABILITY_THRESHOLD`
- `VMG_STABILITY_THRESHOLD`

Run the parameter search:

```bash
python strategy_2026_parameter_search.py
```

The search script writes one metrics file per run and a consolidated:

- [search_summary.csv](parameter_search_output/search_summary.csv)

## Outputs

The strategy script writes:

- `monthly_backtest_speedup.csv`
- `monthly_holdings_speedup.csv`
- `performance_metrics_speedup.json`
- `style_alpha_long_short_backtest_speedup.png`

The parameter-search script writes:

- one `performance_metrics_speedup.json` per run
- `search_summary.csv`

## Example Results

### Reference daily run: 2016-2025

This run is used as the main daily reference because the current evidence suggests that `2014` is a transition period, `2015` is the most defensible regime-change candidate, and `2016` is the point at which the new regime appears to be fully established.

In simple terms:

- `2014` is treated as a transition period because both market structure and fund cross-sectional maturity were changing but not yet stable.
- `2015` is the most defensible regime-change candidate because it sits at the clearest break in both market behavior and strategy performance.
- `2016` is treated as the start of the fully established new regime because the fund universe, style classification, and backtest behavior all look more stable from that point onward.

For the full reasoning, data review, and parameter-search history, see:
- [WORKFLOW.md](WORKFLOW.md)

Parameters:

- `BACKTEST_START_DATE = 2016-01-01`
- `BACKTEST_END_DATE = 2025-12-31`
- `BACKTEST_MIN_STOCK_HOLDING = 70`
- `BACKTEST_LONG_QUANTILE = 0.10`
- `BACKTEST_SHORT_QUANTILE = 0.10`
- `BACKTEST_INCLUDE_ALPHA_BUCKET = False`
- `ROLLING_WINDOW = 245`
- `FULL_SAMPLE_FALLBACK_MAX = 320`
- `P_VALUE_THRESHOLD = 0.05`
- `SMB_STABILITY_THRESHOLD = 1.5`
- `VMG_STABILITY_THRESHOLD = 1.5`

Metrics:

- Strategy Ann. Return: `8.52%`
- Benchmark Ann. Return: `3.98%`
- Strategy Volatility: `6.08%`
- Benchmark Volatility: `16.00%`
- Strategy Sharpe: `1.38`
- Information Ratio: `0.20`
- Strategy Max Drawdown: `-5.39%`
- Monthly Hit Rate: `53.27%`
- Rebalance Months: `107`

Source:

- [performance_metrics_speedup.json](backtest_output_speedup_nnn_s2016_2025_h70_q10_a0_rw245_fb320/performance_metrics_speedup.json)

Backtest chart:

![Daily Style-Alpha Long/Short Backtest](backtest_output_speedup_nnn_s2016_2025_h70_q10_a0_rw245_fb320/style_alpha_long_short_backtest_speedup.png)

### Other Backtest Results

| Sample | Strategy Ann. Return | Benchmark Ann. Return | Strategy Sharpe | Information Ratio | Strategy Max Drawdown | Rebalance Months | Source |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `2015-2025` | `5.91%` | `4.90%` | `1.05` | `-0.00` | `-7.29%` | `119` | [metrics](backtest_output_speedup_15_25/performance_metrics_speedup.json) |
| `2016-2025`, tuned `q=0.12` | `8.29%` | `3.98%` | `1.38` | `0.19` | `-4.31%` | `107` | [metrics](backtest_output_speedup_030_s2016_2025_h60_q12_a0_rw245_fb320/performance_metrics_speedup.json) |
| `2020-2025` | `9.13%` | `-1.49%` | `1.27` | `0.53` | `-5.55%` | `59` | [metrics](parameter_search_output/047_s2020_2025_h65_q10_a0_rw245_fb320/performance_metrics_speedup.json) |


## Important Caveats

This repository is a research project, not a production trading system.

Important limitations:

- The fund universe is still constrained by currently available source lists, so survivorship bias may remain.
- The daily strategy uses current data reconstructions and share-class cleanup rules, which are research choices rather than immutable truths.
- The benchmark is a research benchmark, not a trading implementation benchmark.
- Transaction costs, subscriptions/redemptions, fund dealing cutoffs, and liquidity constraints are not modeled.
- Parameter-search results can be regime-sensitive, so strong short-sample runs should be treated cautiously.

## Related Files

- [style_alpha_long_short_strategy_2026.py](style_alpha_long_short_strategy_2026.py)
  earlier research script used during the daily rebuild
- [get_nav.py](get_nav.py)
  cumulative NAV download script
- [get_stock_daily_data.py](get_stock_daily_data.py)
  stock-level daily input download script

## License / Use

This code is intended for research, auditing, and educational use. Validate the data, assumptions, and execution model before using it in any investment process.
