from pathlib import Path
import bisect
import json
import math
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats
from statsmodels.regression.rolling import RollingOLS


if "__file__" in globals():
    BASE_DIR = Path(__file__).resolve().parent
else:
    BASE_DIR = Path.cwd()
DATA_DIR = BASE_DIR / "data"


FUND_INFO_PATH = DATA_DIR / "fund_info_all.csv"
NAV_PATH = DATA_DIR / "nav_cum.csv"
FACTOR_PATH = DATA_DIR / "CH3_factors_daily_202602.xlsx"
HOLDING_PATH = DATA_DIR / "stock_holding_wide_2000_2025.csv"
HS300_PATH = DATA_DIR / "benchmark" / "sh000300.csv"
TREASURY_PATH = DATA_DIR / "benchmark" / "sh000012.csv"

BACKTEST_START_DATE = "2020-01-01"
BACKTEST_END_DATE = "2025-12-31"
BACKTEST_MIN_STOCK_HOLDING = 70
BACKTEST_LONG_QUANTILE = 0.10
BACKTEST_SHORT_QUANTILE = 0.10
BACKTEST_INCLUDE_ALPHA_BUCKET = False
BACKTEST_OUTPUT_DIR = BASE_DIR / "backtest_output_speedup"

ROLLING_WINDOW = 245
INSUFFICIENT_DATA_THRESHOLD = 245
FULL_SAMPLE_FALLBACK_MAX = 320
P_VALUE_THRESHOLD = 0.05
SMB_STABILITY_THRESHOLD = 1.5
VMG_STABILITY_THRESHOLD = 1.5
PRECOMPUTE_INCLUDE_MISSING_SETUP = False
PRINT_PROGRESS = True


def load_inputs():
    fund_info = pd.read_csv(FUND_INFO_PATH, dtype={"基金代码": str}, encoding="utf-8-sig")
    nav = pd.read_csv(NAV_PATH, encoding="utf-8-sig")
    factor = pd.read_excel(FACTOR_PATH)
    holding = pd.read_csv(HOLDING_PATH, encoding="utf-8-sig")

    fund_info["基金代码"] = fund_info["基金代码"].astype(str).str.zfill(6)
    fund_info["成立日期"] = pd.to_datetime(fund_info["成立日期"], errors="coerce")

    nav["date"] = pd.to_datetime(nav["date"], errors="coerce")
    nav.columns = ["date"] + [str(col).zfill(6) for col in nav.columns[1:]]

    factor["date"] = pd.to_datetime(factor["date"].astype(str), format="%Y%m%d", errors="coerce")

    holding["report_date"] = pd.to_datetime(holding["report_date"], errors="coerce")
    holding.columns = ["report_date"] + [str(col).zfill(6) for col in holding.columns[1:]]

    return fund_info, nav, factor, holding


def filter_nav_remove_ce_shares(nav, fund_info):
    nav = nav.copy()
    nav_fund_cols = [col for col in nav.columns if col != "date"]
    fund_info_nav = fund_info.loc[fund_info["基金代码"].isin(nav_fund_cols)].copy()
    fund_info_nav = fund_info_nav.dropna(subset=["基金全称", "基金简称"])

    fund_info_nav["share_suffix"] = fund_info_nav["基金简称"].str.extract(r"([ACE])$", expand=False)
    fund_info_nav["short_name_base"] = fund_info_nav["基金简称"].str.replace(r"[ACE]$", "", regex=True)

    keep_codes = set(nav_fund_cols)
    for _, group in fund_info_nav.groupby(["基金全称", "short_name_base"], dropna=False):
        if len(group) <= 1:
            continue
        if group["share_suffix"].notna().all() and "A" in group["share_suffix"].values:
            keep_codes -= set(group["基金代码"])
            keep_codes.add(group.loc[group["share_suffix"] == "A", "基金代码"].iloc[0])

    selected_nav_cols = ["date"] + [col for col in nav.columns if col != "date" and col in keep_codes]
    return nav[selected_nav_cols].copy()


def build_precompute_universe(fund_info, nav, end_date, include_missing_setup=False):
    end_date = pd.Timestamp(end_date)
    nav_codes = {col for col in nav.columns if col != "date"}
    fund_meta = fund_info.loc[fund_info["基金代码"].isin(nav_codes)].copy()

    if include_missing_setup:
        mask = fund_meta["成立日期"].isna() | (fund_meta["成立日期"] <= end_date)
    else:
        mask = fund_meta["成立日期"].notna() & (fund_meta["成立日期"] <= end_date)

    return sorted(fund_meta.loc[mask, "基金代码"].unique())


def get_eligible_funds_by_holding(holding, update_date, min_stock_holding=70):
    update_date = pd.Timestamp(update_date)
    holding_filtered = holding.loc[holding["report_date"] <= update_date].copy()

    if holding_filtered.empty:
        return []

    latest_holding_row = holding_filtered.sort_values("report_date").tail(1).iloc[0]
    eligible_funds = []
    for fund_code in holding_filtered.columns:
        if fund_code == "report_date":
            continue
        value = latest_holding_row[fund_code]
        if pd.notna(value) and float(value) >= min_stock_holding:
            eligible_funds.append(fund_code)
    return eligible_funds


def build_full_regression_panel(nav, factor, start_date, end_date, fund_codes):
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)
    selected_cols = ["date"] + [code for code in fund_codes if code in nav.columns]
    nav_filtered = nav.loc[(nav["date"] >= start_date) & (nav["date"] <= end_date), selected_cols].copy()

    nav_long = nav_filtered.melt(
        id_vars="date",
        var_name="fund_code",
        value_name="nav_value",
    )
    nav_long["nav_value"] = pd.to_numeric(nav_long["nav_value"], errors="coerce")
    nav_long = nav_long.dropna(subset=["date", "nav_value"]).copy()
    nav_long = nav_long.sort_values(["fund_code", "date"]).reset_index(drop=True)
    nav_long["fund_return"] = nav_long.groupby("fund_code")["nav_value"].pct_change()

    factor_panel = factor[["date", "rf_dly", "mktrf", "SMB", "VMG"]].copy()
    factor_panel = factor_panel.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    full_panel = nav_long.merge(factor_panel, on="date", how="inner")
    full_panel["excess_return"] = full_panel["fund_return"] - full_panel["rf_dly"]
    full_panel = full_panel.dropna(
        subset=["fund_return", "rf_dly", "mktrf", "SMB", "VMG", "excess_return"]
    ).copy()
    return full_panel.sort_values(["fund_code", "date"]).reset_index(drop=True)


def compute_month_end_rebalance_dates(nav_dates, start_date=None, end_date=None):
    month_end_dates = {}
    for current_date in nav_dates:
        if start_date is not None and current_date < start_date:
            continue
        if end_date is not None and current_date > end_date:
            continue
        month_end_dates[(current_date.year, current_date.month)] = current_date
    return [month_end_dates[key] for key in sorted(month_end_dates)]


def find_next_nav_date(nav_dates, current_date):
    next_position = bisect.bisect_right(list(nav_dates), current_date)
    if next_position >= len(nav_dates):
        return None
    return nav_dates[next_position]


def load_index_close_series(path):
    frame = pd.read_csv(path, parse_dates=["date"])
    series = pd.Series(frame["close"].astype(float).to_numpy(), index=frame["date"])
    return series.sort_index()


def build_forward_return_frame(values_on_execution_dates):
    return values_on_execution_dates.shift(-1).divide(values_on_execution_dates).subtract(1.0)


def build_benchmark_return_series(rebalance_dates, hs300_path, treasury_path):
    rebalance_index = pd.to_datetime(list(rebalance_dates))
    hs300_close = load_index_close_series(hs300_path)
    treasury_close = load_index_close_series(treasury_path)

    boundary_index = hs300_close.index.union(treasury_close.index).union(rebalance_index).sort_values()
    hs300_close = hs300_close.reindex(boundary_index).ffill()
    treasury_close = treasury_close.reindex(boundary_index).ffill()

    hs300_daily_return = hs300_close.pct_change()
    treasury_daily_return = treasury_close.pct_change()
    benchmark_daily_return = 0.8 * hs300_daily_return + 0.2 * treasury_daily_return

    benchmark_rows = []
    for position in range(len(rebalance_index) - 1):
        start_date = rebalance_index[position]
        end_date = rebalance_index[position + 1]
        period_daily_return = benchmark_daily_return.loc[
            (benchmark_daily_return.index > start_date) & (benchmark_daily_return.index <= end_date)
        ].dropna()
        if period_daily_return.empty:
            period_return = np.nan
        else:
            period_return = float((1.0 + period_daily_return).prod() - 1.0)
        benchmark_rows.append({"date": start_date, "benchmark_return": period_return})

    benchmark_rows.append({"date": rebalance_index[-1], "benchmark_return": np.nan})
    benchmark_result = pd.DataFrame(benchmark_rows)
    return pd.Series(benchmark_result["benchmark_return"].to_numpy(), index=benchmark_result["date"])


def classify_style(size_signal, value_signal):
    if size_signal < 0 and value_signal < 0:
        return "Large Growth"
    if size_signal < 0 and value_signal > 0:
        return "Large Value"
    if size_signal > 0 and value_signal < 0:
        return "Small Growth"
    if size_signal > 0 and value_signal > 0:
        return "Small Value"
    return "Alpha"


def precompute_full_sample_snapshots(panel_by_fund, signal_dates):
    rows = []
    signal_dates = pd.to_datetime(signal_dates)
    signal_dates_np = signal_dates.to_numpy(dtype="datetime64[ns]")
    k = 4
    stage_start = time.time()

    for fund_position, (fund_code, fund_df) in enumerate(panel_by_fund.items(), start=1):
        if PRINT_PROGRESS and fund_position % 100 == 0:
            elapsed_minutes = (time.time() - stage_start) / 60.0
            print(f"Full-sample precompute: {fund_position}/{len(panel_by_fund)} | elapsed={elapsed_minutes:.1f}m")

        dates = fund_df["date"].to_numpy(dtype="datetime64[ns]")
        y = fund_df["excess_return"].to_numpy(dtype=float)
        X = np.column_stack(
            [
                np.ones(len(fund_df), dtype=float),
                fund_df["mktrf"].to_numpy(dtype=float),
                fund_df["SMB"].to_numpy(dtype=float),
                fund_df["VMG"].to_numpy(dtype=float),
            ]
        )

        cum_xx = np.einsum("ni,nj->nij", X, X).cumsum(axis=0)
        cum_xy = (X * y[:, None]).cumsum(axis=0)
        cum_yy = (y * y).cumsum()
        positions = np.searchsorted(dates, signal_dates_np, side="right") - 1

        for signal_date, position in zip(signal_dates, positions):
            if position < 0:
                continue

            data_point = int(position + 1)
            row = {
                "signal_date": signal_date,
                "fund_code": fund_code,
                "data_point": data_point,
                "coe_alpha": np.nan,
                "coe_mktrf": np.nan,
                "coe_smb": np.nan,
                "coe_vmg": np.nan,
                "p_value_alpha": np.nan,
                "p_value_mktrf": np.nan,
                "p_value_smb": np.nan,
                "p_value_vmg": np.nan,
            }

            if data_point <= k:
                rows.append(row)
                continue

            xtx = cum_xx[position]
            xty = cum_xy[position]
            yy = float(cum_yy[position])

            try:
                inv_xtx = np.linalg.inv(xtx)
            except np.linalg.LinAlgError:
                rows.append(row)
                continue

            beta = inv_xtx @ xty
            rss = max(yy - float(beta @ xty), 0.0)
            df_resid = data_point - k
            if df_resid <= 0:
                rows.append(row)
                continue

            sigma2 = rss / df_resid
            se = np.sqrt(np.maximum(np.diag(inv_xtx) * sigma2, 0.0))
            with np.errstate(divide="ignore", invalid="ignore"):
                t_stats = beta / se
            p_values = 2 * stats.t.sf(np.abs(t_stats), df=df_resid)

            row.update(
                {
                    "coe_alpha": beta[0],
                    "coe_mktrf": beta[1],
                    "coe_smb": beta[2],
                    "coe_vmg": beta[3],
                    "p_value_alpha": p_values[0],
                    "p_value_mktrf": p_values[1],
                    "p_value_smb": p_values[2],
                    "p_value_vmg": p_values[3],
                }
            )
            rows.append(row)

    result = pd.DataFrame(rows)
    return {
        signal_date: frame.drop(columns="signal_date").reset_index(drop=True)
        for signal_date, frame in result.groupby("signal_date", sort=True)
    }


def precompute_rolling_stability_snapshots(panel_by_fund, signal_dates, rolling_window):
    rows = []
    signal_dates = pd.to_datetime(signal_dates)
    signal_dates_np = signal_dates.to_numpy(dtype="datetime64[ns]")
    stage_start = time.time()

    for fund_position, (fund_code, fund_df) in enumerate(panel_by_fund.items(), start=1):
        if PRINT_PROGRESS and fund_position % 100 == 0:
            elapsed_minutes = (time.time() - stage_start) / 60.0
            print(f"Rolling precompute: {fund_position}/{len(panel_by_fund)} | elapsed={elapsed_minutes:.1f}m")

        if len(fund_df) < rolling_window:
            for signal_date in signal_dates:
                rows.append(
                    {
                        "signal_date": signal_date,
                        "fund_code": fund_code,
                        "reg_count": 0,
                        "smb_mean": np.nan,
                        "smb_sd": np.nan,
                        "smb_sr": np.nan,
                        "vmg_mean": np.nan,
                        "vmg_sd": np.nan,
                        "vmg_sr": np.nan,
                    }
                )
            continue

        X = sm.add_constant(fund_df[["mktrf", "SMB", "VMG"]], has_constant="add")
        y = fund_df["excess_return"]
        rolling_fit = RollingOLS(
            y,
            X,
            window=rolling_window,
            min_nobs=rolling_window,
            missing="drop",
        ).fit(params_only=True)

        rolling_df = pd.DataFrame(
            {
                "date": fund_df["date"].to_numpy(),
                "beta_smb": rolling_fit.params["SMB"].to_numpy(),
                "beta_vmg": rolling_fit.params["VMG"].to_numpy(),
            }
        ).dropna(subset=["beta_smb", "beta_vmg"])

        if rolling_df.empty:
            for signal_date in signal_dates:
                rows.append(
                    {
                        "signal_date": signal_date,
                        "fund_code": fund_code,
                        "reg_count": 0,
                        "smb_mean": np.nan,
                        "smb_sd": np.nan,
                        "smb_sr": np.nan,
                        "vmg_mean": np.nan,
                        "vmg_sd": np.nan,
                        "vmg_sr": np.nan,
                    }
                )
            continue

        rolling_dates = rolling_df["date"].to_numpy(dtype="datetime64[ns]")
        beta_smb = rolling_df["beta_smb"].to_numpy(dtype=float)
        beta_vmg = rolling_df["beta_vmg"].to_numpy(dtype=float)
        cum_smb = beta_smb.cumsum()
        cum_smb_sq = np.square(beta_smb).cumsum()
        cum_vmg = beta_vmg.cumsum()
        cum_vmg_sq = np.square(beta_vmg).cumsum()
        positions = np.searchsorted(rolling_dates, signal_dates_np, side="right") - 1

        for signal_date, position in zip(signal_dates, positions):
            if position < 0:
                rows.append(
                    {
                        "signal_date": signal_date,
                        "fund_code": fund_code,
                        "reg_count": 0,
                        "smb_mean": np.nan,
                        "smb_sd": np.nan,
                        "smb_sr": np.nan,
                        "vmg_mean": np.nan,
                        "vmg_sd": np.nan,
                        "vmg_sr": np.nan,
                    }
                )
                continue

            reg_count = int(position + 1)
            smb_sum = float(cum_smb[position])
            vmg_sum = float(cum_vmg[position])
            smb_mean = smb_sum / reg_count
            vmg_mean = vmg_sum / reg_count

            if reg_count >= 2:
                smb_var = (float(cum_smb_sq[position]) - smb_sum ** 2 / reg_count) / (reg_count - 1)
                vmg_var = (float(cum_vmg_sq[position]) - vmg_sum ** 2 / reg_count) / (reg_count - 1)
                smb_sd = math.sqrt(max(smb_var, 0.0))
                vmg_sd = math.sqrt(max(vmg_var, 0.0))
            else:
                smb_sd = np.nan
                vmg_sd = np.nan

            smb_sr = smb_mean / smb_sd if pd.notna(smb_sd) and smb_sd != 0 else np.nan
            vmg_sr = vmg_mean / vmg_sd if pd.notna(vmg_sd) and vmg_sd != 0 else np.nan

            rows.append(
                {
                    "signal_date": signal_date,
                    "fund_code": fund_code,
                    "reg_count": reg_count,
                    "smb_mean": smb_mean,
                    "smb_sd": smb_sd,
                    "smb_sr": smb_sr,
                    "vmg_mean": vmg_mean,
                    "vmg_sd": vmg_sd,
                    "vmg_sr": vmg_sr,
                }
            )

    result = pd.DataFrame(rows)
    return {
        signal_date: frame.drop(columns="signal_date").reset_index(drop=True)
        for signal_date, frame in result.groupby("signal_date", sort=True)
    }


def build_signal_snapshot(
    signal_date,
    eligible_funds,
    full_snapshot_by_signal,
    rolling_snapshot_by_signal,
):
    if len(eligible_funds) == 0:
        return pd.DataFrame(columns=["fund_code", "alpha", "style"])

    full_snapshot = full_snapshot_by_signal.get(pd.Timestamp(signal_date), pd.DataFrame()).copy()
    if full_snapshot.empty:
        return pd.DataFrame(columns=["fund_code", "alpha", "style"])

    rolling_snapshot = rolling_snapshot_by_signal.get(pd.Timestamp(signal_date), pd.DataFrame()).copy()
    if rolling_snapshot.empty:
        rolling_snapshot = pd.DataFrame(
            columns=["fund_code", "reg_count", "smb_mean", "smb_sd", "smb_sr", "vmg_mean", "vmg_sd", "vmg_sr"]
        )

    snapshot = full_snapshot.merge(rolling_snapshot, on="fund_code", how="left")
    snapshot = snapshot.loc[snapshot["fund_code"].isin(eligible_funds)].copy()
    if snapshot.empty:
        return pd.DataFrame(columns=["fund_code", "alpha", "style"])

    style_values = []
    for _, row in snapshot.iterrows():
        data_point = int(row["data_point"])
        if data_point < INSUFFICIENT_DATA_THRESHOLD:
            style = "Insufficient Data"
        elif data_point <= FULL_SAMPLE_FALLBACK_MAX:
            if (
                pd.notna(row["p_value_smb"])
                and pd.notna(row["p_value_vmg"])
                and row["p_value_smb"] < P_VALUE_THRESHOLD
                and row["p_value_vmg"] < P_VALUE_THRESHOLD
            ):
                style = classify_style(row["coe_smb"], row["coe_vmg"])
            else:
                style = "Alpha"
        else:
            if (
                pd.notna(row["smb_sr"])
                and pd.notna(row["vmg_sr"])
                and abs(row["smb_sr"]) > SMB_STABILITY_THRESHOLD
                and abs(row["vmg_sr"]) > VMG_STABILITY_THRESHOLD
            ):
                style = classify_style(row["smb_sr"], row["vmg_sr"])
            else:
                style = "Alpha"
        style_values.append(style)

    snapshot["style"] = style_values
    snapshot["alpha"] = snapshot["coe_alpha"]
    return snapshot


def select_portfolio_holdings(snapshot, long_quantile, short_quantile, include_alpha_bucket):
    eligible = snapshot.loc[snapshot["style"] != "Insufficient Data"].copy()
    eligible = eligible.loc[eligible["alpha"].notna()].copy()
    if not include_alpha_bucket:
        eligible = eligible.loc[eligible["style"] != "Alpha"].copy()

    holding_rows = []
    active_buckets = []

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
            holding_rows.append(
                {
                    "fund_code": fund_code,
                    "style": style,
                    "side": "long",
                    "weight": long_weight,
                }
            )

        for fund_code in short_codes:
            holding_rows.append(
                {
                    "fund_code": fund_code,
                    "style": style,
                    "side": "short",
                    "weight": short_weight,
                }
            )

    return pd.DataFrame(holding_rows)


def compute_side_return(holdings, period_return_row, side):
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


def compute_metrics(backtest):
    periods_per_year = 12

    def annualized_return(cumulative_series, periods):
        total_return = float(cumulative_series.iloc[-1])
        years = periods / periods_per_year
        return total_return ** (1.0 / years) - 1.0

    def annualized_volatility(returns):
        return float(returns.std(ddof=1) * math.sqrt(periods_per_year))

    def sharpe_ratio(returns):
        volatility = returns.std(ddof=1)
        if not math.isfinite(volatility) or volatility == 0:
            return np.nan
        return float(returns.mean() / volatility * math.sqrt(periods_per_year))

    def max_drawdown(cumulative_series):
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


def plot_results(backtest, metrics, save_path):
    fig = plt.figure(figsize=(14, 10), facecolor="#0f1117")
    grid = fig.add_gridspec(3, 1, hspace=0.10, height_ratios=[2, 1, 1])

    text_color = "#e0e0e0"
    grid_color = "#2a2a3a"
    benchmark_color = "#6b7db3"
    strategy_color = "#f0c040"
    long_color = "#00d4aa"
    short_color = "#ff4d6d"

    plot_curve = pd.DataFrame(
        {
            "holding_end_date": backtest["holding_end_date"],
            "benchmark_curve": backtest["benchmark_cumulative"] - 1.0,
            "strategy_curve": backtest["strategy_cumulative"] - 1.0,
        }
    )
    initial_row = pd.DataFrame(
        {
            "holding_end_date": [backtest["trade_date"].min()],
            "benchmark_curve": [0.0],
            "strategy_curve": [0.0],
        }
    )
    plot_curve = pd.concat([initial_row, plot_curve], ignore_index=True)

    ax1 = fig.add_subplot(grid[0])
    ax1.plot(plot_curve["holding_end_date"], plot_curve["benchmark_curve"], color=benchmark_color, lw=1.5, label="Benchmark 80/20")
    ax1.plot(plot_curve["holding_end_date"], plot_curve["strategy_curve"], color=strategy_color, lw=1.5, label="Style-Alpha Long/Short")
    ax1.set_title("Monthly Style-Alpha Long/Short Backtest (Speedup)", color=text_color, fontsize=14, pad=12)
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
    plt.close(fig)


def run_backtest_speedup():
    total_start = time.time()

    print("Loading inputs...")
    stage_start = time.time()
    fund_info, nav, factor, holding = load_inputs()
    print(f"Loading inputs done | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    print("Applying A/C/E share-class filter...")
    stage_start = time.time()
    nav = filter_nav_remove_ce_shares(nav, fund_info)
    holding = holding[["report_date"] + [col for col in holding.columns if col != "report_date" and col in nav.columns]].copy()
    print(f"A/C/E filter done | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    print("Building precompute universe...")
    stage_start = time.time()
    precompute_universe = build_precompute_universe(
        fund_info=fund_info,
        nav=nav,
        end_date=BACKTEST_END_DATE,
        include_missing_setup=PRECOMPUTE_INCLUDE_MISSING_SETUP,
    )
    print(f"Precompute universe size: {len(precompute_universe)}")
    print(f"Precompute universe ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    print("Building full daily regression panel...")
    stage_start = time.time()
    full_panel = build_full_regression_panel(
        nav=nav,
        factor=factor,
        start_date=BACKTEST_START_DATE,
        end_date=BACKTEST_END_DATE,
        fund_codes=precompute_universe,
    )
    print(f"Full panel rows: {len(full_panel)}")
    print(f"Full daily regression panel ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    panel_by_fund = {
        fund_code: frame.sort_values("date").reset_index(drop=True)
        for fund_code, frame in full_panel.groupby("fund_code", sort=True)
    }

    nav_frame = nav.set_index("date").sort_index()
    nav_dates = list(nav_frame.index)
    rebalance_dates = compute_month_end_rebalance_dates(
        nav_dates,
        start_date=pd.Timestamp(BACKTEST_START_DATE),
        end_date=pd.Timestamp(BACKTEST_END_DATE),
    )

    backtest_end_timestamp = pd.Timestamp(BACKTEST_END_DATE)
    signal_dates = []
    execution_dates = []
    for signal_date in rebalance_dates:
        execution_date = find_next_nav_date(nav_dates, signal_date)
        if execution_date is None or execution_date > backtest_end_timestamp:
            continue
        signal_dates.append(signal_date)
        execution_dates.append(execution_date)

    if len(signal_dates) < 1:
        raise ValueError("Not enough signal/execution pairs to run the backtest.")

    print("Precomputing monthly full-sample snapshots...")
    stage_start = time.time()
    full_snapshot_by_signal = precompute_full_sample_snapshots(panel_by_fund, signal_dates)
    print(f"Monthly full-sample snapshots ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")
    print("Precomputing monthly rolling-stability snapshots...")
    stage_start = time.time()
    rolling_snapshot_by_signal = precompute_rolling_stability_snapshots(panel_by_fund, signal_dates, ROLLING_WINDOW)
    print(f"Monthly rolling-stability snapshots ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    print("Precomputing eligible funds by signal date...")
    stage_start = time.time()
    eligible_by_signal = {}
    precompute_universe_set = set(precompute_universe)
    for signal_date in signal_dates:
        eligible = get_eligible_funds_by_holding(
            holding=holding,
            update_date=signal_date,
            min_stock_holding=BACKTEST_MIN_STOCK_HOLDING,
        )
        eligible_by_signal[signal_date] = [code for code in eligible if code in precompute_universe_set]
    print(f"Eligible-fund snapshots ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    period_boundary_dates = execution_dates + [backtest_end_timestamp]
    boundary_index = pd.to_datetime(period_boundary_dates)
    nav_on_boundary_dates = nav_frame.reindex(boundary_index).ffill()
    forward_fund_returns = build_forward_return_frame(nav_on_boundary_dates)
    benchmark_returns = build_benchmark_return_series(period_boundary_dates, HS300_PATH, TREASURY_PATH)

    backtest_rows = []
    holding_rows = []

    for position, current_date in enumerate(signal_dates):
        if PRINT_PROGRESS:
            elapsed_minutes = (time.time() - total_start) / 60.0
            print(
                f"Backtest progress: {position + 1}/{len(signal_dates)} | "
                f"signal_date={pd.Timestamp(current_date).date()} | elapsed={elapsed_minutes:.1f}m"
            )

        trade_date = execution_dates[position]
        if position < len(signal_dates) - 1:
            next_signal_date = signal_dates[position + 1]
            exit_date = execution_dates[position + 1]
        else:
            next_signal_date = pd.NaT
            exit_date = backtest_end_timestamp
        eligible_funds = eligible_by_signal.get(current_date, [])

        snapshot = build_signal_snapshot(
            signal_date=current_date,
            eligible_funds=eligible_funds,
            full_snapshot_by_signal=full_snapshot_by_signal,
            rolling_snapshot_by_signal=rolling_snapshot_by_signal,
        )
        holdings = select_portfolio_holdings(
            snapshot=snapshot,
            long_quantile=BACKTEST_LONG_QUANTILE,
            short_quantile=BACKTEST_SHORT_QUANTILE,
            include_alpha_bucket=BACKTEST_INCLUDE_ALPHA_BUCKET,
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
                "signal_date": current_date,
                "trade_date": trade_date,
                "next_signal_date": next_signal_date,
                "exit_date": exit_date,
                "rebalance_date": current_date,
                "holding_end_date": exit_date,
                "long_return": long_return,
                "short_return": short_return,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "long_count": int((holdings["side"] == "long").sum()),
                "short_count": int((holdings["side"] == "short").sum()),
                "style_count": int(holdings["style"].nunique()),
            }
        )

        period_holdings = holdings.merge(snapshot[["fund_code", "alpha"]], on="fund_code", how="left")
        period_holdings.insert(0, "signal_date", current_date)
        period_holdings.insert(1, "trade_date", trade_date)
        period_holdings.insert(2, "next_signal_date", next_signal_date)
        period_holdings.insert(3, "exit_date", exit_date)
        period_holdings.insert(4, "rebalance_date", current_date)
        period_holdings.insert(5, "holding_end_date", exit_date)
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
    metrics = compute_metrics(backtest)

    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    backtest.to_csv(BACKTEST_OUTPUT_DIR / "monthly_backtest_speedup.csv", index=False, encoding="utf-8-sig")
    holding_detail.to_csv(BACKTEST_OUTPUT_DIR / "monthly_holdings_speedup.csv", index=False, encoding="utf-8-sig")
    with (BACKTEST_OUTPUT_DIR / "performance_metrics_speedup.json").open("w", encoding="utf-8") as handle:
        json.dump(metrics, handle, ensure_ascii=False, indent=2)
    plot_results(backtest, metrics, BACKTEST_OUTPUT_DIR / "style_alpha_long_short_backtest_speedup.png")

    print("\nBacktest metrics")
    for key, value in metrics.items():
        print(f"  {key:<24} {value}")
    print(f"Total elapsed={(time.time() - total_start) / 60.0:.1f}m")
    print(f"\nSaved outputs to {BACKTEST_OUTPUT_DIR}")


if __name__ == "__main__":
    run_backtest_speedup()
