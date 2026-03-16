# Fund Style Strategy Model

## Purpose

This document explains the strategy itself: what problem it solves, how the model works, how funds are classified, and how the ranking and portfolio rules are built on top of that classification.

The core objective is to make mutual fund comparisons meaningful. A fund should be compared against peers with a similar investment style, not against the entire fund universe.

## Background and Motivation

Public mutual funds usually disclose their holdings only once per quarter. That creates a timing problem for style analysis and risk monitoring:

- the fund may already have changed positions well before the next holdings disclosure
- the stated category in the prospectus or marketing material may not match the assets the fund is actually holding
- the fund's real risk exposure may therefore drift away from its claimed style before investors can observe the holdings directly

This is exactly why return-based style analysis is useful.

Even when full holdings are not available in real time, fund NAV is published much more frequently. Because NAV reflects the economic result of the underlying portfolio, it can be used with a factor model to infer the fund's actual risk exposure from realized returns.

In this framework, the Fama-French three-factor model is not only a performance model. It is also a practical tool for identifying what kind of risk a fund is truly taking:

- market exposure
- size exposure
- value-versus-growth exposure

That makes it possible to estimate a fund's real style between holdings disclosure dates, rather than waiting for the next quarterly portfolio snapshot.

## Why Fund Classification Is Needed

There are two main reasons.

Reason 1: meaningful comparison

Different funds face different risk exposures and different return opportunities because they differ in investor base, asset mix, investment strategy, and style. Ranking or rating funds only makes sense when the comparison is made inside a comparable peer group.

For example, it would not be meaningful to compare a stock fund and a fixed-income fund only by raw return and conclude that the higher-return fund is better. Their opportunity set and risk budget are fundamentally different.

Reason 2: investment style drift

A fund's stated objective often does not fully describe how it is actually invested. Two funds may both claim to be "growth" funds, while one mainly owns established blue-chip companies and the other mainly owns smaller, more aggressive companies. On paper they sound similar, but their actual risk exposure can be very different.

This is why the strategy uses a quantitative model to identify a fund's real style from behavior rather than relying only on labels. By grouping funds according to actual exposure, investors can:

- compare performance more fairly
- assess risk more accurately
- detect style drift sooner
- build more coherent peer groups and portfolios

The main objective is therefore simple:

- reveal the true style (risk exposure) of each fund
- make mutual fund comparisons meaningful by classifying funds according to their actual and persistent risk exposure

## Strategy Idea

The strategy has two layers:

1. Identify each fund's actual investment style from realized returns rather than relying only on the stated prospectus style.
2. Rank funds only within the same style bucket, using manager-specific alpha as the selection signal.

This solves two common problems:

- funds with different style exposures should not be ranked together
- a fund's style may drift over time, so the classification must be dynamic rather than static

## Target Universe

The strategy is designed for actively managed equity-biased mutual funds.

It assumes two separate data layers:

- stock-level market data: 
  - ALL A shares
  - used to build the risk factors
- mutual-fund-level data: 
  - active Equity Funds in China (with equity holdings above 70% at the end of the previous quarter.)
  - used to estimate each fund's exposures, alpha, and style stability

## Model

The strategy uses a Fama-French three-factor framework:

`Ri - Rf = alpha + beta_m * (Rm - Rf) + beta_s * SMB + beta_h * HML`

Where:

- `Ri` is the fund's weekly return
- `Rf` is the weekly risk-free rate
- `Rm` is the market return
- `SMB` captures size exposure
- `HML` captures value-versus-growth exposure
- `alpha` is the fund's excess return that is not explained by market, size, or value style tilts

The intuition is simple:

- a positive `SMB` loading suggests small-cap exposure
- a negative `SMB` loading suggests large-cap exposure
- a positive `HML` loading suggests value exposure
- a negative `HML` loading suggests growth exposure

## Factor Construction

The stock-level factor model is built from the full equity market universe.

The market is sorted in two dimensions:

- size
  bottom 30%, middle 40%, top 30%
- book-to-market
  bottom 30%, middle 40%, top 30%

That produces nine stock portfolios. Weekly portfolio returns are then aggregated into the three factor series:

- `Rm`
  market-cap-weighted return of the eligible stock universe
- `SMB`
  average small-cap portfolio return minus average big-cap portfolio return
- `HML`
  average high book-to-market portfolio return minus average low book-to-market portfolio return

This setup gives the model enough structure to separate market beta from persistent size and risk tilts.

## Why Rolling Regression

Fund style is not assumed to be constant.

A manager may shift from small cap to large cap, from growth to value, or move between style-driven investing and more idiosyncratic stock picking. To detect that behavior, the strategy uses rolling regression rather than a single static regression.

The standard design is:

- rolling window: 52 weeks
- if a fund has less than 52 usable observations, run one regression on the full available sample

This creates a time series of factor loadings rather than one fixed set of coefficients.

## Style Classification Framework

The strategy maps each fund into five categories:

- `Large Growth`
- `Large Value`
- `Small Growth`
- `Small Value`
- `Alpha`

The first four categories are reserved for funds whose style exposure is stable enough to be trusted. A fund is assigned to `Alpha` when the data suggests that its style is unstable, drifting, or not clearly explained by persistent size and value loadings.

## Classification Method I

The first classification idea is based on the direction of factor exposure through time.

Decision logic:

- `SMB > 0` implies small-cap
- `SMB < 0` implies large-cap
- `HML > 0` implies value
- `HML < 0` implies growth

For short-history funds:

- run one regression
- if a factor coefficient is statistically significant, use its sign
- if the style signal is not statistically significant, classify as `Alpha`

For longer-history funds:

- run rolling regressions
- count how often each coefficient is positive or negative
- if the sign pattern is balanced or unstable, classify as `Alpha`
- otherwise classify according to the dominant sign

This method is intuitive, but it can be noisy when coefficients change magnitude a lot over time.

## Classification Method II

The second classification idea uses the Sharpe ratio of the rolling regression coefficients.

For each factor:

`Coefficient Sharpe Ratio = mean(rolling coefficient) / sd(rolling coefficient)`

Interpretation:

- a large absolute Sharpe ratio means the coefficient is stable and persistent
- a small absolute Sharpe ratio means the coefficient is noisy or unstable

This is a direct way to turn style persistence into a measurable stability score.

For short-history funds:

- run one regression on all available data
- if both `SMB` and `HML` are significant, classify by the signs of the coefficients
- otherwise classify as `Alpha`

For longer-history funds:

- run rolling 52-week regressions
- compute the coefficient Sharpe ratios for `SMB` and `HML`
- if both absolute Sharpe ratios exceed the benchmark, classify by the signs of the coefficients
- otherwise classify as `Alpha`

This is the cleaner production rule because it combines direction and stability in one framework.

## Benchmark Choice

The practical benchmark for style stability is:

- `|SMB coefficient Sharpe ratio| > 1`
- `|HML coefficient Sharpe ratio| > 1`

The idea behind this threshold is that once the coefficient Sharpe ratio is comfortably above 1 in absolute value, the style signal tends to stop flipping signs frequently. In other words, the fund's style is not only present, but persistent enough to be investable as a style label.

## Style Mapping

Once the size and value signals are both stable enough, the style label is assigned by sign:

- `SMB < 0`, `HML < 0` -> `Large Growth`
- `SMB < 0`, `HML > 0` -> `Large Value`
- `SMB > 0`, `HML < 0` -> `Small Growth`
- `SMB > 0`, `HML > 0` -> `Small Value`

If either style dimension is not stable enough, the fund is assigned to:

- `Alpha`

This means `Alpha` should not be read as "best fund." It means the fund is not well described by a stable size-value style box.

## Alpha Ranking Logic

After style classification, the strategy ranks funds within each style bucket using regression alpha.

Alpha is estimated from the same three-factor model, using the fund's history from either:

- inception, or
- the current manager's effective start date

This matters because the goal is to measure the active contribution of the current manager, not dilute it with a prior manager's history.

Interpretation:

- higher alpha means more return beyond what is explained by market, size, and value exposures
- alpha is therefore treated as the manager-skill signal inside each style peer group

## Portfolio Construction

The portfolio rule is simple:

- group funds by style
- rank funds within each style by alpha
- go long the top 10% in each style bucket
- go short the bottom 10% in each style bucket

This keeps the long-short comparison style-neutral. The strategy is not trying to bet that one style will outperform another. It is trying to identify stronger and weaker managers inside the same style segment.

## Monitoring and Reclassification

Style labels should be refreshed on a regular schedule because mutual fund style can drift.

The monitoring logic is:

- refresh the regression window with the newest data
- recompute rolling exposures
- re-evaluate stability
- update the style bucket before re-ranking alpha

This helps avoid stale peer groups and keeps the ranking aligned with the fund's current behavior rather than its old label.

## What Makes the Strategy Distinct

The strategy is not just a standard factor regression and not just a standard peer ranking.

Its distinguishing features are:

- style is inferred from realized behavior, not only from declared mandate
- style stability matters as much as style direction
- unstable or drifting funds are separated into `Alpha` instead of being forced into a misleading box
- alpha ranking is done only after style normalization
- the final portfolio construction is style-relative rather than cross-style

## Practical Interpretation

In plain English, the strategy says:

- first find out what kind of fund each manager is actually running
- only trust that label if the style is stable through time
- then compare managers only against others who run the same kind of portfolio
- finally, select the strongest and weakest funds within each style bucket using alpha

That is the logic linking classification, ranking, and trading into one coherent process.
