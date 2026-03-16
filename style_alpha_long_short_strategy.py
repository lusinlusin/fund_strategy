from __future__ import annotations

import argparse
import bisect
import json
import math
from datetime import date
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from fund_strategy_pipeline import (
    build_factor_panel,
    build_factor_returns,
    build_nav_lookup,
    classify_style,
    load_fund_metadata,
    load_rf_series,
    ols_with_pvalues,
)


# ── 1. Data ──────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monthly style-alpha long-short fund strategy backtest.")
    parser.add_argument(
        "--quarter-dir",
        default="data_input/2017Q1",
        help="Input folder containing nav.csv, fundname.xlsx, factor files, and Rf.xlsx.",
    )
    parser.add_argument(
        "--hs300-path",
        default="akshare_index_data/sh000300.csv",
        help="CSV file for the CSI 300 index.",
    )
    parser.add_argument(
        "--treasury-path",
        default="akshare_index_data/sh000012.csv",
        help="CSV file for the SSE Treasury index.",
    )
    parser.add_argument(
        "--output-dir",
        default="strategy_backtest_output",
        help="Directory used to save the backtest outputs.",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="Optional backtest start date in YYYY-MM-DD format. Defaults to the first month with tradable signals.",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="Optional backtest end date in YYYY-MM-DD format. Defaults to the last available rebalance date.",
    )
    parser.add_argument(
        "--history-weeks",
        type=int,
        default=448,
        help="Rolling weekly lookback window used to estimate style exposure and alpha.",
    )
    parser.add_argument(
        "--min-observations",
        type=int,
        default=52,
        help="Minimum weekly observations required before a fund can enter a style bucket.",
    )
    parser.add_argument(
        "--long-quantile",
        type=float,
        default=0.10,
        help="Share of funds to hold long within each style bucket.",
    )
    parser.add_argument(
        "--short-quantile",
        type=float,
        default=0.10,
        help="Share of funds to hold short within each style bucket.",
    )
    parser.add_argument(
        "--exclude-alpha-bucket",
        action="store_true",
        help="Exclude funds classified as Alpha from the portfolio construction step.",
    )
    return parser.parse_args()


def parse_iso_date(text: str) -> date:
    return date.fromisoformat(text)


def compute_month_end_rebalance_dates(
    nav_dates: Sequence[date],
    start_date: date | None = None,
    end_date: date | None = None,
) -> List[date]:
    month_end_dates: Dict[tuple[int, int], date] = {}
    for current_date in nav_dates:
        if start_date is not None and current_date < start_date:
            continue
        if end_date is not None and current_date > end_date:
            continue
        month_end_dates[(current_date.year, current_date.month)] = current_date
    return [month_end_dates[key] for key in sorted(month_end_dates)]


def find_next_nav_date(nav_dates: Sequence[date], current_date: date) -> date | None:
    next_position = bisect.bisect_right(list(nav_dates), current_date)
    if next_position >= len(nav_dates):
        return None
    return nav_dates[next_position]


def load_nav_frame(quarter_dir: Path) -> tuple[pd.DataFrame, List[date]]:
    fund_metadata = load_fund_metadata(quarter_dir)
    nav_dates, ordered_nav_series = build_nav_lookup(quarter_dir)
    nav_frame = pd.DataFrame(
        {
            item["fund_code"]: ordered_nav_series[int(item["legacy_order"]) - 1]
            for item in fund_metadata
        },
        index=pd.to_datetime(nav_dates),
    )
    return nav_frame, nav_dates


def load_index_close_series(path: Path) -> pd.Series:
    frame = pd.read_csv(path, parse_dates=["date"])
    series = pd.Series(frame["close"].astype(float).to_numpy(), index=frame["date"])
    series = series.sort_index()
    return series


def build_forward_return_frame(values_on_rebalance_dates: pd.DataFrame) -> pd.DataFrame:
    return values_on_rebalance_dates.shift(-1).divide(values_on_rebalance_dates).subtract(1.0)


def build_benchmark_return_series(
    rebalance_dates: Sequence[date],
    hs300_path: Path,
    treasury_path: Path,
) -> pd.Series:
    rebalance_index = pd.to_datetime(list(rebalance_dates))
    hs300_close = load_index_close_series(hs300_path).reindex(rebalance_index, method="ffill")
    treasury_close = load_index_close_series(treasury_path).reindex(rebalance_index, method="ffill")

    hs300_return = hs300_close.shift(-1).divide(hs300_close).subtract(1.0)
    treasury_return = treasury_close.shift(-1).divide(treasury_close).subtract(1.0)
    return 0.8 * hs300_return + 0.2 * treasury_return


# ── 2. Signals ────────────────────────────────────────────────────────────────


def extract_snapshot_model_data(
    nav_series: np.ndarray,
    nav_dates: Sequence[date],
    factor_dates: Sequence[date],
    rf: np.ndarray,
    rm_rf: np.ndarray,
    smb: np.ndarray,
    hml: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    prev_nav = np.concatenate(([np.nan], nav_series[:-1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        full_returns = np.log(nav_series / prev_nav)

    nav_index = {current_date: index for index, current_date in enumerate(nav_dates)}
    aligned_returns = np.array([full_returns[nav_index[current_date]] for current_date in factor_dates], dtype=float)
    dates_array = np.array(factor_dates, dtype=object)
    model_mask = np.isfinite(aligned_returns) & np.isfinite(rf) & np.isfinite(rm_rf) & np.isfinite(smb) & np.isfinite(hml)
    model_dates = dates_array[model_mask]
    model_y = aligned_returns[model_mask] - rf[model_mask]
    model_x = np.column_stack([np.ones(np.sum(model_mask)), rm_rf[model_mask], smb[model_mask], hml[model_mask]])
    return model_dates, model_y, model_x


def build_monthly_snapshot(
    analysis_end_date: date,
    factor_rows: List[Dict[str, object]],
    rf_by_date: Dict[date, float],
    nav_dates: Sequence[date],
    nav_frame: pd.DataFrame,
    history_weeks: int,
    min_observations: int,
) -> pd.DataFrame:
    factor_panel = build_factor_panel(
        factor_rows=factor_rows,
        nav_dates=list(nav_dates),
        rf_by_date=rf_by_date,
        analysis_end_date=analysis_end_date,
        history_weeks=history_weeks,
    )
    if not factor_panel:
        return pd.DataFrame(columns=["fund_code", "style", "alpha", "data_point", "coe_smb", "coe_hml"])

    factor_dates = [row["_date"] for row in factor_panel]
    rf = np.array([row["return_compound"] for row in factor_panel], dtype=float)
    rm_rf = np.array([row["rm"] - row["return_compound"] for row in factor_panel], dtype=float)
    smb = np.array([row["smb"] for row in factor_panel], dtype=float)
    hml = np.array([row["hml"] for row in factor_panel], dtype=float)

    snapshot_rows: List[Dict[str, object]] = []
    for fund_code in nav_frame.columns:
        nav_series = nav_frame[fund_code].to_numpy(dtype=float)
        _, model_y, model_x = extract_snapshot_model_data(
            nav_series=nav_series,
            nav_dates=nav_dates,
            factor_dates=factor_dates,
            rf=rf,
            rm_rf=rm_rf,
            smb=smb,
            hml=hml,
        )
        data_point = len(model_y)
        row = {
            "fund_code": fund_code,
            "style": "Insufficient Data",
            "alpha": np.nan,
            "data_point": data_point,
            "coe_smb": np.nan,
            "coe_hml": np.nan,
            "p_value_smb": np.nan,
            "p_value_hml": np.nan,
        }
        if data_point < min_observations:
            snapshot_rows.append(row)
            continue
        try:
            coefficients, p_values = ols_with_pvalues(model_y, model_x)
        except np.linalg.LinAlgError:
            snapshot_rows.append(row)
            continue

        row.update(
            {
                "alpha": coefficients[0],
                "coe_smb": coefficients[2],
                "coe_hml": coefficients[3],
                "p_value_smb": p_values[2],
                "p_value_hml": p_values[3],
            }
        )
        if math.isfinite(p_values[2]) and math.isfinite(p_values[3]) and p_values[2] < 0.05 and p_values[3] < 0.05:
            row["style"] = classify_style(coefficients[2], coefficients[3])
        else:
            row["style"] = "Alpha"
        snapshot_rows.append(row)

    snapshot = pd.DataFrame(snapshot_rows)
    snapshot = snapshot.sort_values(["style", "alpha"], ascending=[True, False], na_position="last").reset_index(drop=True)
    return snapshot


def select_portfolio_holdings(
    snapshot: pd.DataFrame,
    long_quantile: float,
    short_quantile: float,
    include_alpha_bucket: bool,
) -> pd.DataFrame:
    eligible = snapshot.loc[snapshot["style"] != "Insufficient Data"].copy()
    eligible = eligible.loc[eligible["alpha"].notna()].copy()
    if not include_alpha_bucket:
        eligible = eligible.loc[eligible["style"] != "Alpha"].copy()

    holding_rows: List[Dict[str, object]] = []
    active_buckets: List[tuple[str, List[str], List[str]]] = []

    for style, bucket in eligible.groupby("style"):
        bucket = bucket.sort_values("alpha", ascending=False).reset_index(drop=True)
        bucket_size = len(bucket)
        long_count = max(1, math.ceil(bucket_size * long_quantile))
        short_count = max(1, math.ceil(bucket_size * short_quantile))
        selected_count = min(long_count, short_count, bucket_size // 2)
        if selected_count < 1:
            continue

        long_bucket = bucket.head(selected_count)
        short_bucket = bucket.tail(selected_count)
        if set(long_bucket["fund_code"]).intersection(set(short_bucket["fund_code"])):
            continue
        active_buckets.append(
            (
                style,
                long_bucket["fund_code"].tolist(),
                short_bucket["fund_code"].tolist(),
            )
        )

    if not active_buckets:
        return pd.DataFrame(columns=["fund_code", "style", "side", "weight"])

    bucket_weight = 1.0 / len(active_buckets)
    for style, long_codes, short_codes in active_buckets:
        long_weight = bucket_weight / len(long_codes)
        short_weight = -bucket_weight / len(short_codes)
        for fund_code in long_codes:
            holding_rows.append({"fund_code": fund_code, "style": style, "side": "long", "weight": long_weight})
        for fund_code in short_codes:
            holding_rows.append({"fund_code": fund_code, "style": style, "side": "short", "weight": short_weight})

    return pd.DataFrame(holding_rows)


# ── 3. Backtest ───────────────────────────────────────────────────────────────


def compute_side_return(holdings: pd.DataFrame, period_return_row: pd.Series, side: str) -> float:
    side_holdings = holdings.loc[holdings["side"] == side].copy()
    if side_holdings.empty:
        return np.nan

    side_holdings["period_return"] = side_holdings["fund_code"].map(period_return_row.to_dict())
    side_holdings = side_holdings.loc[side_holdings["period_return"].notna()].copy()
    if side_holdings.empty:
        return np.nan

    if side == "long":
        weights = side_holdings["weight"].to_numpy(dtype=float)
        weights = weights / weights.sum()
        return float(np.sum(weights * side_holdings["period_return"].to_numpy(dtype=float)))

    weights = np.abs(side_holdings["weight"].to_numpy(dtype=float))
    weights = weights / weights.sum()
    return float(-np.sum(weights * side_holdings["period_return"].to_numpy(dtype=float)))


def run_backtest(
    quarter_dir: Path,
    hs300_path: Path,
    treasury_path: Path,
    start_date: date | None,
    end_date: date | None,
    history_weeks: int,
    min_observations: int,
    long_quantile: float,
    short_quantile: float,
    include_alpha_bucket: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    factor_rows = build_factor_returns(quarter_dir)
    rf_by_date = load_rf_series(quarter_dir)
    nav_frame, nav_dates = load_nav_frame(quarter_dir)

    rebalance_dates = compute_month_end_rebalance_dates(nav_dates, start_date=start_date, end_date=end_date)
    if len(rebalance_dates) < 2:
        raise ValueError("Not enough month-end dates to run the backtest.")

    signal_dates: List[date] = []
    execution_dates: List[date] = []
    for signal_date in rebalance_dates:
        execution_date = find_next_nav_date(nav_dates, signal_date)
        if execution_date is None:
            continue
        signal_dates.append(signal_date)
        execution_dates.append(execution_date)

    if len(signal_dates) < 2:
        raise ValueError("Not enough signal/execution pairs to run the backtest without look-ahead bias.")

    execution_index = pd.to_datetime(execution_dates)
    nav_on_execution = nav_frame.reindex(execution_index)
    forward_fund_returns = build_forward_return_frame(nav_on_execution)
    benchmark_returns = build_benchmark_return_series(execution_dates, hs300_path=hs300_path, treasury_path=treasury_path)

    backtest_rows: List[Dict[str, object]] = []
    holding_rows: List[Dict[str, object]] = []

    for position, current_date in enumerate(signal_dates[:-1]):
        next_signal_date = signal_dates[position + 1]
        trade_date = execution_dates[position]
        exit_date = execution_dates[position + 1]
        snapshot = build_monthly_snapshot(
            analysis_end_date=current_date,
            factor_rows=factor_rows,
            rf_by_date=rf_by_date,
            nav_dates=nav_dates,
            nav_frame=nav_frame,
            history_weeks=history_weeks,
            min_observations=min_observations,
        )
        holdings = select_portfolio_holdings(
            snapshot=snapshot,
            long_quantile=long_quantile,
            short_quantile=short_quantile,
            include_alpha_bucket=include_alpha_bucket,
        )
        if holdings.empty:
            continue

        period_return_row = forward_fund_returns.loc[pd.Timestamp(trade_date)]
        long_return = compute_side_return(holdings, period_return_row, side="long")
        short_return = compute_side_return(holdings, period_return_row, side="short")
        if not math.isfinite(long_return) or not math.isfinite(short_return):
            continue

        benchmark_return = float(benchmark_returns.loc[pd.Timestamp(trade_date)])
        strategy_return = long_return + short_return

        backtest_rows.append(
            {
                "signal_date": current_date.isoformat(),
                "trade_date": trade_date.isoformat(),
                "next_signal_date": next_signal_date.isoformat(),
                "exit_date": exit_date.isoformat(),
                "rebalance_date": current_date.isoformat(),
                "holding_end_date": exit_date.isoformat(),
                "long_return": long_return,
                "short_return": short_return,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "long_count": int((holdings["side"] == "long").sum()),
                "short_count": int((holdings["side"] == "short").sum()),
                "style_count": int(holdings["style"].nunique()),
            }
        )

        snapshot_alpha = snapshot[["fund_code", "alpha"]].copy()
        period_holdings = holdings.merge(snapshot_alpha, on="fund_code", how="left")
        period_holdings.insert(0, "signal_date", current_date.isoformat())
        period_holdings.insert(1, "trade_date", trade_date.isoformat())
        period_holdings.insert(2, "next_signal_date", next_signal_date.isoformat())
        period_holdings.insert(3, "exit_date", exit_date.isoformat())
        period_holdings.insert(4, "rebalance_date", current_date.isoformat())
        period_holdings.insert(5, "holding_end_date", exit_date.isoformat())
        holding_rows.extend(period_holdings.to_dict("records"))

    backtest = pd.DataFrame(backtest_rows)
    if backtest.empty:
        raise ValueError("The strategy did not produce any tradable monthly portfolios.")

    backtest["signal_date"] = pd.to_datetime(backtest["signal_date"])
    backtest["trade_date"] = pd.to_datetime(backtest["trade_date"])
    backtest["next_signal_date"] = pd.to_datetime(backtest["next_signal_date"])
    backtest["exit_date"] = pd.to_datetime(backtest["exit_date"])
    backtest["rebalance_date"] = pd.to_datetime(backtest["rebalance_date"])
    backtest["holding_end_date"] = pd.to_datetime(backtest["holding_end_date"])
    backtest["strategy_cumulative"] = (1.0 + backtest["strategy_return"]).cumprod()
    backtest["benchmark_cumulative"] = (1.0 + backtest["benchmark_return"]).cumprod()
    backtest["excess_return"] = backtest["strategy_return"] - backtest["benchmark_return"]
    holding_detail = pd.DataFrame(holding_rows)
    return backtest, holding_detail


# ── 4. Metrics ────────────────────────────────────────────────────────────────


def compute_metrics(backtest: pd.DataFrame) -> Dict[str, str]:
    periods_per_year = 12

    def annualized_return(cumulative_series: pd.Series, periods: int) -> float:
        total_return = float(cumulative_series.iloc[-1])
        years = periods / periods_per_year
        return total_return ** (1.0 / years) - 1.0

    def annualized_volatility(returns: pd.Series) -> float:
        return float(returns.std(ddof=1) * math.sqrt(periods_per_year))

    def sharpe_ratio(returns: pd.Series) -> float:
        volatility = returns.std(ddof=1)
        if not math.isfinite(volatility) or volatility == 0:
            return np.nan
        return float(returns.mean() / volatility * math.sqrt(periods_per_year))

    def max_drawdown(cumulative_series: pd.Series) -> float:
        rolling_max = cumulative_series.cummax()
        drawdown = cumulative_series / rolling_max - 1.0
        return float(drawdown.min())

    strategy_returns = backtest["strategy_return"]
    benchmark_returns = backtest["benchmark_return"]
    excess_returns = backtest["excess_return"]

    return {
        "Strategy Ann. Return": f"{annualized_return(backtest['strategy_cumulative'], len(backtest)):.2%}",
        "Benchmark Ann. Return": f"{annualized_return(backtest['benchmark_cumulative'], len(backtest)):.2%}",
        "Strategy Volatility": f"{annualized_volatility(strategy_returns):.2%}",
        "Benchmark Volatility": f"{annualized_volatility(benchmark_returns):.2%}",
        "Strategy Sharpe": f"{sharpe_ratio(strategy_returns):.2f}",
        "Benchmark Sharpe": f"{sharpe_ratio(benchmark_returns):.2f}",
        "Information Ratio": f"{sharpe_ratio(excess_returns):.2f}",
        "Strategy Max Drawdown": f"{max_drawdown(backtest['strategy_cumulative']):.2%}",
        "Benchmark Max Drawdown": f"{max_drawdown(backtest['benchmark_cumulative']):.2%}",
        "Average Monthly Excess": f"{excess_returns.mean():.2%}",
        "Monthly Hit Rate": f"{(excess_returns > 0).mean():.2%}",
        "Rebalance Months": str(len(backtest)),
    }


# ── 5. Plot ───────────────────────────────────────────────────────────────────


def plot_results(backtest: pd.DataFrame, metrics: Dict[str, str], save_path: Path) -> None:
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec

    fig = plt.figure(figsize=(14, 10), facecolor="#0f1117")
    grid = gridspec.GridSpec(3, 1, figure=fig, hspace=0.10, height_ratios=[2, 1, 1])

    text_color = "#e0e0e0"
    grid_color = "#2a2a3a"
    benchmark_color = "#6b7db3"
    strategy_color = "#f0c040"
    long_color = "#00d4aa"
    short_color = "#ff4d6d"

    ax1 = fig.add_subplot(grid[0])
    ax1.plot(backtest["holding_end_date"], backtest["benchmark_cumulative"], color=benchmark_color, lw=1.5, label="Benchmark 80/20")
    ax1.plot(backtest["holding_end_date"], backtest["strategy_cumulative"], color=strategy_color, lw=1.5, label="Style-Alpha Long/Short")
    ax1.set_title("Monthly Style-Alpha Long/Short Backtest", color=text_color, fontsize=14, pad=12)
    ax1.set_ylabel("Cumulative Return", color=text_color)
    ax1.legend(facecolor="#1a1a2e", edgecolor=grid_color, labelcolor=text_color, fontsize=9)
    ax1.set_facecolor("#0f1117")
    ax1.tick_params(colors=text_color, labelbottom=False)
    ax1.grid(color=grid_color, linewidth=0.5)
    for spine in ax1.spines.values():
        spine.set_edgecolor(grid_color)

    ax2 = fig.add_subplot(grid[1], sharex=ax1)
    ax2.bar(backtest["holding_end_date"], backtest["long_return"], color=long_color, alpha=0.75, width=20, label="Long Leg")
    ax2.bar(backtest["holding_end_date"], backtest["short_return"], color=short_color, alpha=0.65, width=20, label="Short Leg")
    ax2.axhline(0.0, color=grid_color, linewidth=1.0)
    ax2.set_ylabel("Monthly Return", color=text_color)
    ax2.legend(facecolor="#1a1a2e", edgecolor=grid_color, labelcolor=text_color, fontsize=8)
    ax2.set_facecolor("#0f1117")
    ax2.tick_params(colors=text_color, labelbottom=False)
    ax2.grid(color=grid_color, linewidth=0.5)
    for spine in ax2.spines.values():
        spine.set_edgecolor(grid_color)

    ax3 = fig.add_subplot(grid[2], sharex=ax1)
    drawdown = backtest["strategy_cumulative"] / backtest["strategy_cumulative"].cummax() - 1.0
    ax3.fill_between(backtest["holding_end_date"], drawdown, 0.0, color=short_color, alpha=0.4, label="Strategy Drawdown")
    ax3.set_ylabel("Drawdown", color=text_color)
    ax3.set_xlabel("Date", color=text_color)
    ax3.set_facecolor("#0f1117")
    ax3.tick_params(colors=text_color)
    ax3.grid(color=grid_color, linewidth=0.5)
    for spine in ax3.spines.values():
        spine.set_edgecolor(grid_color)

    metrics_text = "  |  ".join(f"{key}: {value}" for key, value in metrics.items())
    fig.text(
        0.5,
        0.01,
        metrics_text,
        ha="center",
        fontsize=7.5,
        color="#aaaacc",
        bbox=dict(facecolor="#1a1a2e", edgecolor=grid_color, boxstyle="round,pad=0.4"),
    )

    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close()


# ── 6. Main ───────────────────────────────────────────────────────────────────


def main() -> int:
    args = parse_args()
    quarter_dir = Path(args.quarter_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    hs300_path = Path(args.hs300_path).resolve()
    treasury_path = Path(args.treasury_path).resolve()
    start_date = parse_iso_date(args.start_date) if args.start_date else None
    end_date = parse_iso_date(args.end_date) if args.end_date else None

    backtest, holding_detail = run_backtest(
        quarter_dir=quarter_dir,
        hs300_path=hs300_path,
        treasury_path=treasury_path,
        start_date=start_date,
        end_date=end_date,
        history_weeks=args.history_weeks,
        min_observations=args.min_observations,
        long_quantile=args.long_quantile,
        short_quantile=args.short_quantile,
        include_alpha_bucket=not args.exclude_alpha_bucket,
    )

    metrics = compute_metrics(backtest)

    output_dir.mkdir(parents=True, exist_ok=True)
    backtest.to_csv(output_dir / "monthly_backtest.csv", index=False, encoding="utf-8-sig")
    holding_detail.to_csv(output_dir / "monthly_holdings.csv", index=False, encoding="utf-8-sig")
    with (output_dir / "performance_metrics.json").open("w", encoding="utf-8") as handle:
        json.dump(metrics, handle, ensure_ascii=False, indent=2)
    plot_results(backtest, metrics, output_dir / "style_alpha_long_short_backtest.png")

    print("\nBacktest metrics")
    for key, value in metrics.items():
        print(f"  {key:<24} {value}")
    print(f"\nSaved outputs to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
