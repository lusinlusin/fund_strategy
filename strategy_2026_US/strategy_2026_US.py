from pathlib import Path
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


RETURNS_PATH = DATA_DIR / "sf_monthly_returns.csv"
FACTOR_PATH = DATA_DIR / "ff5_mom_factors.csv"

BACKTEST_START_DATE = "1990-01-01"
BACKTEST_END_DATE = "2025-12-31"
BACKTEST_MIN_STOCK_HOLDING = 70
BACKTEST_LONG_QUANTILE = 0.10
BACKTEST_SHORT_QUANTILE = 0
BACKTEST_INCLUDE_ALPHA_BUCKET = False
BACKTEST_OUTPUT_DIR = BASE_DIR / "backtest_output_annual"

ROLLING_WINDOW = 36
INSUFFICIENT_DATA_THRESHOLD = 36
FULL_SAMPLE_FALLBACK_MAX = 48
ALPHA_TRAILING_WINDOW = 120 # months used to estimate the ranking alpha; None = expanding full history
P_VALUE_THRESHOLD = 0.05
SMB_STABILITY_THRESHOLD = 1.5
HML_STABILITY_THRESHOLD = 1.5
PRINT_PROGRESS = True

# Factors included in the regression model. Pick any subset of the columns available in
# ff5_mom_factors.csv: mkt_rf, smb, hml, rmw, cma, mom. The regression intercept is the
# ranking alpha; every listed factor is controlled for when estimating it.
FACTOR_COLUMNS = ["mkt_rf", "smb", "hml", "mom"]
# Which factor loadings define the size and value axes of the style classification.
# Both MUST appear in FACTOR_COLUMNS.
SIZE_FACTOR = "smb"
VALUE_FACTOR = "hml"


def normalize_fund_code(series):
    numeric = pd.to_numeric(series, errors="coerce")
    result = series.astype("string")
    numeric_mask = numeric.notna()
    result.loc[numeric_mask] = numeric.loc[numeric_mask].round().astype("Int64").astype(str)
    return result.astype(str)


def to_bool(series):
    if series.dtype == bool:
        return series.fillna(False)
    return series.astype(str).str.lower().isin({"true", "1", "yes"})


def load_inputs():
    returns = pd.read_csv(
        RETURNS_PATH,
        dtype={"crsp_fundno": str},
        parse_dates=["caldt", "first_offer_dt", "first_5m_tna_dt"],
        low_memory=False,
    )
    factor = pd.read_csv(FACTOR_PATH, parse_dates=["date"])

    returns = returns.rename(
        columns={
            "crsp_fundno": "fund_code",
            "caldt": "date",
            "mret": "fund_return",
        }
    )
    returns["fund_code"] = normalize_fund_code(returns["fund_code"])
    returns["date"] = pd.to_datetime(returns["date"], errors="coerce")
    returns["fund_return"] = pd.to_numeric(returns["fund_return"], errors="coerce")
    returns["mtna"] = pd.to_numeric(returns.get("mtna"), errors="coerce")
    returns["per_com"] = pd.to_numeric(returns.get("per_com"), errors="coerce")

    for column in [
        "no_load",
        "per_com_ge_70",
        "age_ge_36m",
        "after_first_5m_tna",
        "is_active",
        "is_us_domestic_equity",
        "in_alpha_history",
        "in_paper_sample",
    ]:
        if column in returns.columns:
            returns[column] = to_bool(returns[column])

    returns = returns.dropna(subset=["fund_code", "date", "fund_return"]).copy()
    returns = returns.sort_values(["fund_code", "date"]).reset_index(drop=True)

    factor["date"] = pd.to_datetime(factor["date"], errors="coerce")
    missing_factors = [column for column in FACTOR_COLUMNS if column not in factor.columns]
    if missing_factors:
        available = [c for c in factor.columns if c not in ("date", "rf")]
        raise ValueError(
            f"FACTOR_COLUMNS requests {missing_factors} which are not in {FACTOR_PATH.name}. "
            f"Available factors: {available}"
        )
    for column in ["rf"] + FACTOR_COLUMNS:
        factor[column] = pd.to_numeric(factor[column], errors="coerce")
    factor = factor.dropna(subset=["date", "rf"] + FACTOR_COLUMNS).copy()
    factor = factor.sort_values("date").reset_index(drop=True)

    return returns, factor


def build_precompute_universe(returns, end_date):
    end_date = pd.Timestamp(end_date)
    historical = returns.loc[returns["date"] <= end_date].copy()
    if "in_alpha_history" in historical.columns:
        historical = historical.loc[historical["in_alpha_history"].eq(True)].copy()
    return sorted(historical["fund_code"].dropna().unique())


def get_eligible_funds_by_signal(returns, signal_date, min_stock_holding=70):
    signal_date = pd.Timestamp(signal_date)
    current = returns.loc[returns["date"] == signal_date].copy()
    if current.empty:
        return []

    current = current.sort_values(["fund_code", "date"]).drop_duplicates("fund_code", keep="last")

    mask = pd.Series(True, index=current.index)
    if "in_paper_sample" in current.columns:
        mask &= current["in_paper_sample"].eq(True)
    if "per_com" in current.columns:
        mask &= current["per_com"].ge(min_stock_holding)
    elif "per_com_ge_70" in current.columns and min_stock_holding == 70:
        mask &= current["per_com_ge_70"].eq(True)

    return current.loc[mask, "fund_code"].tolist()


def build_equal_weight_benchmark_series(returns, min_stock_holding=70):
    benchmark_universe = returns.dropna(subset=["date", "fund_return"]).copy()

    mask = pd.Series(True, index=benchmark_universe.index)
    if "in_paper_sample" in benchmark_universe.columns:
        mask &= benchmark_universe["in_paper_sample"].eq(True)
    if "per_com" in benchmark_universe.columns:
        mask &= benchmark_universe["per_com"].ge(min_stock_holding)
    elif "per_com_ge_70" in benchmark_universe.columns and min_stock_holding == 70:
        mask &= benchmark_universe["per_com_ge_70"].eq(True)

    benchmark_universe = benchmark_universe.loc[mask].copy()
    return benchmark_universe.groupby("date")["fund_return"].mean().sort_index()


def build_value_weight_benchmark_series(returns, min_stock_holding=70):
    benchmark_universe = returns.dropna(subset=["date", "fund_return"]).copy()
    benchmark_universe = benchmark_universe.sort_values(["fund_code", "date"])
    # Weight period-t returns by beginning-of-period (prior month) TNA, not contemporaneous
    # TNA, so a fund's own return does not inflate its own weight. Lag is computed over the
    # fund's full history before filtering, so a one-month gap in eligibility keeps the weight.
    benchmark_universe["mtna_lag"] = benchmark_universe.groupby("fund_code")["mtna"].shift(1)

    mask = pd.Series(True, index=benchmark_universe.index)
    if "in_paper_sample" in benchmark_universe.columns:
        mask &= benchmark_universe["in_paper_sample"].eq(True)
    if "per_com" in benchmark_universe.columns:
        mask &= benchmark_universe["per_com"].ge(min_stock_holding)
    elif "per_com_ge_70" in benchmark_universe.columns and min_stock_holding == 70:
        mask &= benchmark_universe["per_com_ge_70"].eq(True)
    mask &= benchmark_universe["mtna_lag"].gt(0)

    benchmark_universe = benchmark_universe.loc[mask].copy()

    def _weighted_mean(group):
        weights = group["mtna_lag"].to_numpy(dtype=float)
        total = weights.sum()
        if total <= 0:
            return np.nan
        rets = group["fund_return"].to_numpy(dtype=float)
        return float(np.dot(weights, rets) / total)

    return benchmark_universe.groupby("date")[["mtna_lag", "fund_return"]].apply(_weighted_mean).sort_index()


def build_full_regression_panel(returns, factor, end_date, fund_codes):
    end_date = pd.Timestamp(end_date)
    fund_code_set = set(fund_codes)
    returns_filtered = returns.loc[
        (returns["date"] <= end_date) & (returns["fund_code"].isin(fund_code_set))
    ].copy()

    returns_filtered["month"] = returns_filtered["date"].dt.to_period("M")
    factor_panel = factor[["date", "rf"] + FACTOR_COLUMNS].copy()
    factor_panel["month"] = factor_panel["date"].dt.to_period("M")
    factor_panel = factor_panel.drop(columns=["date"])

    full_panel = returns_filtered.merge(factor_panel, on="month", how="inner")
    full_panel["excess_return"] = full_panel["fund_return"] - full_panel["rf"]
    full_panel = full_panel.dropna(
        subset=["fund_return", "rf"] + FACTOR_COLUMNS + ["excess_return"]
    ).copy()
    return full_panel.sort_values(["fund_code", "date"]).reset_index(drop=True)


def compute_month_end_rebalance_dates(monthly_dates, start_date=None, end_date=None):
    month_end_dates = {}
    for current_date in monthly_dates:
        if start_date is not None and current_date < start_date:
            continue
        if end_date is not None and current_date > end_date:
            continue
        month_end_dates[(current_date.year, current_date.month)] = current_date
    return [month_end_dates[key] for key in sorted(month_end_dates)]


def compute_year_end_rebalance_dates(monthly_dates, start_date=None, end_date=None):
    year_end_dates = {}
    for current_date in monthly_dates:
        if start_date is not None and current_date < start_date:
            continue
        if end_date is not None and current_date > end_date:
            continue
        year_end_dates[current_date.year] = current_date
    return [year_end_dates[key] for key in sorted(year_end_dates)]


def build_annual_holding_schedule(monthly_dates, signal_dates, end_date):
    end_date = pd.Timestamp(end_date)
    date_positions = {date: position for position, date in enumerate(monthly_dates)}
    schedule = {}

    for position, signal_date in enumerate(signal_dates):
        signal_position = date_positions.get(signal_date)
        if signal_position is None:
            continue

        if position < len(signal_dates) - 1:
            next_signal_date = signal_dates[position + 1]
            period_end_date = min(pd.Timestamp(next_signal_date), end_date)
        else:
            next_signal_date = pd.NaT
            period_end_date = end_date

        holding_dates = [
            current_date
            for current_date in monthly_dates[signal_position + 1:]
            if current_date <= period_end_date
        ]
        if holding_dates:
            schedule[signal_date] = {
                "next_signal_date": next_signal_date,
                "holding_dates": holding_dates,
            }

    return schedule


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


def precompute_full_sample_snapshots(panel_by_fund, signal_dates, trailing_window=None):
    rows = []
    signal_dates = pd.to_datetime(signal_dates)
    signal_dates_np = signal_dates.to_numpy(dtype="datetime64[ns]")
    factor_names = list(FACTOR_COLUMNS)
    k = len(factor_names) + 1  # +1 for the intercept (alpha)
    nan_estimates = {}
    for name in ["alpha"] + factor_names:
        nan_estimates[f"coe_{name}"] = np.nan
        nan_estimates[f"p_value_{name}"] = np.nan
    stage_start = time.time()

    for fund_position, (fund_code, fund_df) in enumerate(panel_by_fund.items(), start=1):
        if PRINT_PROGRESS and fund_position % 100 == 0:
            elapsed_minutes = (time.time() - stage_start) / 60.0
            print(f"Full-sample precompute: {fund_position}/{len(panel_by_fund)} | elapsed={elapsed_minutes:.1f}m")

        dates = fund_df["date"].to_numpy(dtype="datetime64[ns]")
        y = fund_df["excess_return"].to_numpy(dtype=float)
        X = np.column_stack(
            [np.ones(len(fund_df), dtype=float)]
            + [fund_df[name].to_numpy(dtype=float) for name in factor_names]
        )

        cum_xx = np.einsum("ni,nj->nij", X, X).cumsum(axis=0)
        cum_xy = (X * y[:, None]).cumsum(axis=0)
        cum_yy = (y * y).cumsum()
        positions = np.searchsorted(dates, signal_dates_np, side="right") - 1

        for signal_date, position in zip(signal_dates, positions):
            if position < 0:
                continue

            data_point = int(position + 1)
            row = {"signal_date": signal_date, "fund_code": fund_code, "data_point": data_point}
            row.update(nan_estimates)

            if data_point <= k:
                rows.append(row)
                continue

            if trailing_window is not None and data_point > trailing_window:
                lo = position - trailing_window + 1
                xtx = cum_xx[position] - cum_xx[lo - 1]
                xty = cum_xy[position] - cum_xy[lo - 1]
                yy = float(cum_yy[position] - cum_yy[lo - 1])
                n_obs = trailing_window
            else:
                xtx = cum_xx[position]
                xty = cum_xy[position]
                yy = float(cum_yy[position])
                n_obs = data_point

            try:
                inv_xtx = np.linalg.inv(xtx)
            except np.linalg.LinAlgError:
                rows.append(row)
                continue

            beta = inv_xtx @ xty
            rss = max(yy - float(beta @ xty), 0.0)
            df_resid = n_obs - k
            if df_resid <= 0:
                rows.append(row)
                continue

            sigma2 = rss / df_resid
            se = np.sqrt(np.maximum(np.diag(inv_xtx) * sigma2, 0.0))
            with np.errstate(divide="ignore", invalid="ignore"):
                t_stats = beta / se
            p_values = 2 * stats.t.sf(np.abs(t_stats), df=df_resid)

            estimates = {"coe_alpha": beta[0], "p_value_alpha": p_values[0]}
            for idx, name in enumerate(factor_names, start=1):
                estimates[f"coe_{name}"] = beta[idx]
                estimates[f"p_value_{name}"] = p_values[idx]
            row.update(estimates)
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

    empty_row = {
        "reg_count": 0,
        "size_mean": np.nan,
        "size_sd": np.nan,
        "size_sr": np.nan,
        "value_mean": np.nan,
        "value_sd": np.nan,
        "value_sr": np.nan,
    }

    for fund_position, (fund_code, fund_df) in enumerate(panel_by_fund.items(), start=1):
        if PRINT_PROGRESS and fund_position % 100 == 0:
            elapsed_minutes = (time.time() - stage_start) / 60.0
            print(f"Rolling precompute: {fund_position}/{len(panel_by_fund)} | elapsed={elapsed_minutes:.1f}m")

        if len(fund_df) < rolling_window:
            for signal_date in signal_dates:
                rows.append({"signal_date": signal_date, "fund_code": fund_code, **empty_row})
            continue

        X = sm.add_constant(fund_df[FACTOR_COLUMNS], has_constant="add")
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
                "beta_size": rolling_fit.params[SIZE_FACTOR].to_numpy(),
                "beta_value": rolling_fit.params[VALUE_FACTOR].to_numpy(),
            }
        ).dropna(subset=["beta_size", "beta_value"])

        if rolling_df.empty:
            for signal_date in signal_dates:
                rows.append({"signal_date": signal_date, "fund_code": fund_code, **empty_row})
            continue

        rolling_dates = rolling_df["date"].to_numpy(dtype="datetime64[ns]")
        beta_size = rolling_df["beta_size"].to_numpy(dtype=float)
        beta_value = rolling_df["beta_value"].to_numpy(dtype=float)
        cum_size = beta_size.cumsum()
        cum_size_sq = np.square(beta_size).cumsum()
        cum_value = beta_value.cumsum()
        cum_value_sq = np.square(beta_value).cumsum()
        positions = np.searchsorted(rolling_dates, signal_dates_np, side="right") - 1

        for signal_date, position in zip(signal_dates, positions):
            if position < 0:
                rows.append({"signal_date": signal_date, "fund_code": fund_code, **empty_row})
                continue

            reg_count = int(position + 1)
            size_sum = float(cum_size[position])
            value_sum = float(cum_value[position])
            size_mean = size_sum / reg_count
            value_mean = value_sum / reg_count

            if reg_count >= 2:
                size_var = (float(cum_size_sq[position]) - size_sum ** 2 / reg_count) / (reg_count - 1)
                value_var = (float(cum_value_sq[position]) - value_sum ** 2 / reg_count) / (reg_count - 1)
                size_sd = math.sqrt(max(size_var, 0.0))
                value_sd = math.sqrt(max(value_var, 0.0))
            else:
                size_sd = np.nan
                value_sd = np.nan

            size_sr = size_mean / size_sd if pd.notna(size_sd) and size_sd != 0 else np.nan
            value_sr = value_mean / value_sd if pd.notna(value_sd) and value_sd != 0 else np.nan

            rows.append(
                {
                    "signal_date": signal_date,
                    "fund_code": fund_code,
                    "reg_count": reg_count,
                    "size_mean": size_mean,
                    "size_sd": size_sd,
                    "size_sr": size_sr,
                    "value_mean": value_mean,
                    "value_sd": value_sd,
                    "value_sr": value_sr,
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
            columns=["fund_code", "reg_count", "size_mean", "size_sd", "size_sr", "value_mean", "value_sd", "value_sr"]
        )

    snapshot = full_snapshot.merge(rolling_snapshot, on="fund_code", how="left")
    snapshot = snapshot.loc[snapshot["fund_code"].isin(eligible_funds)].copy()
    if snapshot.empty:
        return pd.DataFrame(columns=["fund_code", "alpha", "style"])

    size_coe_col = f"coe_{SIZE_FACTOR}"
    value_coe_col = f"coe_{VALUE_FACTOR}"
    size_p_col = f"p_value_{SIZE_FACTOR}"
    value_p_col = f"p_value_{VALUE_FACTOR}"

    style_values = []
    for _, row in snapshot.iterrows():
        data_point = int(row["data_point"])
        if data_point < INSUFFICIENT_DATA_THRESHOLD:
            style = "Insufficient Data"
        elif data_point <= FULL_SAMPLE_FALLBACK_MAX:
            if (
                pd.notna(row[size_p_col])
                and pd.notna(row[value_p_col])
                and row[size_p_col] < P_VALUE_THRESHOLD
                and row[value_p_col] < P_VALUE_THRESHOLD
            ):
                style = classify_style(row[size_coe_col], row[value_coe_col])
            else:
                style = "Alpha"
        else:
            if (
                pd.notna(row["size_sr"])
                and pd.notna(row["value_sr"])
                and abs(row["size_sr"]) > SMB_STABILITY_THRESHOLD
                and abs(row["value_sr"]) > HML_STABILITY_THRESHOLD
            ):
                style = classify_style(row["size_sr"], row["value_sr"])
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
    include_short = short_quantile > 0

    for style, bucket in eligible.groupby("style"):
        bucket = bucket.sort_values("alpha", ascending=False).reset_index(drop=True)
        bucket_size = len(bucket)
        long_count = max(1, math.ceil(bucket_size * long_quantile))
        if include_short:
            short_count = max(1, math.ceil(bucket_size * short_quantile))
            selected_count = min(long_count, short_count, bucket_size // 2)
        else:
            selected_count = min(long_count, bucket_size)
        if selected_count < 1:
            continue

        long_bucket = bucket.head(selected_count)
        if include_short:
            short_bucket = bucket.tail(selected_count)
            if set(long_bucket["fund_code"]).intersection(set(short_bucket["fund_code"])):
                continue
            short_codes = short_bucket["fund_code"].tolist()
        else:
            short_codes = []

        active_buckets.append(
            (
                style,
                long_bucket["fund_code"].tolist(),
                short_codes,
            )
        )

    if not active_buckets:
        return pd.DataFrame(columns=["fund_code", "style", "side", "weight"])

    bucket_weight = 1.0 / len(active_buckets)
    for style, long_codes, short_codes in active_buckets:
        long_weight = bucket_weight / len(long_codes)

        for fund_code in long_codes:
            holding_rows.append(
                {
                    "fund_code": fund_code,
                    "style": style,
                    "side": "long",
                    "weight": long_weight,
                }
            )

        if not include_short:
            continue

        short_weight = -bucket_weight / len(short_codes)
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
        if side == "short":
            return 0.0
        return np.nan

    side_holdings["period_return"] = side_holdings["fund_code"].map(period_return_row.to_dict())
    side_holdings = side_holdings.loc[side_holdings["period_return"].notna()].copy()
    if side_holdings.empty:
        if side == "short":
            return 0.0
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
        if total_return <= 0 or years <= 0:
            return np.nan
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
    vw_benchmark_returns = backtest["vw_benchmark_return"]
    vw_excess_returns = backtest["vw_excess_return"]

    return {
        "Strategy Ann. Return": f"{annualized_return(backtest['strategy_cumulative'], len(backtest)):.2%}",
        "Benchmark (EW) Ann. Return": f"{annualized_return(backtest['benchmark_cumulative'], len(backtest)):.2%}",
        "Benchmark (VW) Ann. Return": f"{annualized_return(backtest['vw_benchmark_cumulative'], len(backtest)):.2%}",
        "Strategy Volatility": f"{annualized_volatility(strategy_returns):.2%}",
        "Benchmark (EW) Volatility": f"{annualized_volatility(benchmark_returns):.2%}",
        "Benchmark (VW) Volatility": f"{annualized_volatility(vw_benchmark_returns):.2%}",
        "Strategy Sharpe": f"{sharpe_ratio(strategy_returns):.2f}",
        "Benchmark (EW) Sharpe": f"{sharpe_ratio(benchmark_returns):.2f}",
        "Benchmark (VW) Sharpe": f"{sharpe_ratio(vw_benchmark_returns):.2f}",
        "Information Ratio (vs EW)": f"{sharpe_ratio(excess_returns):.2f}",
        "Information Ratio (vs VW)": f"{sharpe_ratio(vw_excess_returns):.2f}",
        "Strategy Max Drawdown": f"{max_drawdown(backtest['strategy_cumulative']):.2%}",
        "Benchmark (EW) Max Drawdown": f"{max_drawdown(backtest['benchmark_cumulative']):.2%}",
        "Benchmark (VW) Max Drawdown": f"{max_drawdown(backtest['vw_benchmark_cumulative']):.2%}",
        "Average Monthly Excess (vs EW)": f"{excess_returns.mean():.2%}",
        "Average Monthly Excess (vs VW)": f"{vw_excess_returns.mean():.2%}",
        "Monthly Hit Rate (vs EW)": f"{(excess_returns > 0).mean():.2%}",
        "Monthly Hit Rate (vs VW)": f"{(vw_excess_returns > 0).mean():.2%}",
        "Return Months": str(len(backtest)),
        "Rebalance Years": str(backtest["rebalance_date"].nunique()),
    }


def plot_results(backtest, metrics, save_path):
    fig = plt.figure(figsize=(14, 10), facecolor="#0f1117")
    grid = fig.add_gridspec(3, 1, hspace=0.10, height_ratios=[2, 1, 1])

    text_color = "#e0e0e0"
    grid_color = "#2a2a3a"
    benchmark_color = "#6b7db3"
    vw_benchmark_color = "#a06be0"
    strategy_color = "#f0c040"
    long_color = "#00d4aa"
    short_color = "#ff4d6d"

    plot_curve = pd.DataFrame(
        {
            "holding_end_date": backtest["holding_end_date"],
            "benchmark_curve": backtest["benchmark_cumulative"] - 1.0,
            "vw_benchmark_curve": backtest["vw_benchmark_cumulative"] - 1.0,
            "strategy_curve": backtest["strategy_cumulative"] - 1.0,
        }
    )
    initial_row = pd.DataFrame(
        {
            "holding_end_date": [backtest["trade_date"].min()],
            "benchmark_curve": [0.0],
            "vw_benchmark_curve": [0.0],
            "strategy_curve": [0.0],
        }
    )
    plot_curve = pd.concat([initial_row, plot_curve], ignore_index=True)

    ax1 = fig.add_subplot(grid[0])
    ax1.plot(plot_curve["holding_end_date"], plot_curve["benchmark_curve"], color=benchmark_color, lw=1.5, label="Equal-Weight Funds")
    ax1.plot(plot_curve["holding_end_date"], plot_curve["vw_benchmark_curve"], color=vw_benchmark_color, lw=1.5, label="Value-Weight Funds (TNA)")
    ax1.plot(plot_curve["holding_end_date"], plot_curve["strategy_curve"], color=strategy_color, lw=1.5, label="Style-Alpha Long/Short")
    ax1.set_title("Annual-Rebalanced U.S. Style-Alpha Long/Short Backtest", color=text_color, fontsize=14, pad=12)
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

    metrics_items = [f"{key}: {value}" for key, value in metrics.items()]
    split_point = (len(metrics_items) + 1) // 2
    metrics_text = "  |  ".join(metrics_items[:split_point]) + "\n" + "  |  ".join(metrics_items[split_point:])
    fig.text(
        0.5,
        0.01,
        metrics_text,
        ha="center",
        va="bottom",
        fontsize=7.5,
        color="#aaaacc",
        linespacing=1.6,
        bbox=dict(facecolor="#1a1a2e", edgecolor=grid_color, boxstyle="round,pad=0.4"),
    )

    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def run_backtest_monthly():
    total_start = time.time()

    for axis_name, axis_factor in (("SIZE_FACTOR", SIZE_FACTOR), ("VALUE_FACTOR", VALUE_FACTOR)):
        if axis_factor not in FACTOR_COLUMNS:
            raise ValueError(
                f"{axis_name}='{axis_factor}' must be one of FACTOR_COLUMNS {FACTOR_COLUMNS} "
                "because the style classification reads its loading."
            )
    print(f"Model factors: {FACTOR_COLUMNS} | size axis={SIZE_FACTOR} | value axis={VALUE_FACTOR}")

    print("Loading inputs...")
    stage_start = time.time()
    returns, factor = load_inputs()
    print(f"Loading inputs done | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    print("Building precompute universe...")
    stage_start = time.time()
    precompute_universe = build_precompute_universe(
        returns=returns,
        end_date=BACKTEST_END_DATE,
    )
    print(f"Precompute universe size: {len(precompute_universe)}")
    print(f"Precompute universe ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    print("Building full monthly regression panel...")
    stage_start = time.time()
    full_panel = build_full_regression_panel(
        returns=returns,
        factor=factor,
        end_date=BACKTEST_END_DATE,
        fund_codes=precompute_universe,
    )
    print(f"Full panel rows: {len(full_panel)}")
    print(f"Full monthly regression panel ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    panel_by_fund = {
        fund_code: frame.sort_values("date").reset_index(drop=True)
        for fund_code, frame in full_panel.groupby("fund_code", sort=True)
    }

    return_frame = (
        returns.pivot_table(index="date", columns="fund_code", values="fund_return", aggfunc="last")
        .sort_index()
    )
    benchmark_returns = build_equal_weight_benchmark_series(
        returns=returns,
        min_stock_holding=BACKTEST_MIN_STOCK_HOLDING,
    )
    vw_benchmark_returns = build_value_weight_benchmark_series(
        returns=returns,
        min_stock_holding=BACKTEST_MIN_STOCK_HOLDING,
    )
    monthly_dates = list(return_frame.index)
    signal_dates = compute_year_end_rebalance_dates(
        monthly_dates,
        start_date=pd.Timestamp(BACKTEST_START_DATE),
        end_date=pd.Timestamp(BACKTEST_END_DATE),
    )

    backtest_end_timestamp = pd.Timestamp(BACKTEST_END_DATE)
    holding_schedule = build_annual_holding_schedule(
        monthly_dates=monthly_dates,
        signal_dates=signal_dates,
        end_date=backtest_end_timestamp,
    )
    signal_dates = [signal_date for signal_date in signal_dates if signal_date in holding_schedule]

    if len(signal_dates) < 1:
        raise ValueError("Not enough signal/holding-period pairs to run the backtest.")

    print("Precomputing monthly full-sample snapshots...")
    stage_start = time.time()
    full_snapshot_by_signal = precompute_full_sample_snapshots(panel_by_fund, signal_dates, ALPHA_TRAILING_WINDOW)
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
        eligible = get_eligible_funds_by_signal(
            returns=returns,
            signal_date=signal_date,
            min_stock_holding=BACKTEST_MIN_STOCK_HOLDING,
        )
        eligible_by_signal[signal_date] = [code for code in eligible if code in precompute_universe_set]
    print(f"Eligible-fund snapshots ready | elapsed={(time.time() - stage_start) / 60.0:.1f}m")

    backtest_rows = []
    holding_rows = []

    for position, current_date in enumerate(signal_dates):
        if PRINT_PROGRESS:
            elapsed_minutes = (time.time() - total_start) / 60.0
            print(
                f"Backtest progress: {position + 1}/{len(signal_dates)} | "
                f"signal_date={pd.Timestamp(current_date).date()} | elapsed={elapsed_minutes:.1f}m"
            )

        trade_date = current_date
        schedule_item = holding_schedule[current_date]
        next_signal_date = schedule_item["next_signal_date"]
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

        for exit_date in schedule_item["holding_dates"]:
            period_return_row = return_frame.loc[pd.Timestamp(exit_date)]
            long_return = compute_side_return(holdings, period_return_row, side="long")
            short_return = compute_side_return(holdings, period_return_row, side="short")
            if not math.isfinite(long_return) or not math.isfinite(short_return):
                continue

            benchmark_return = benchmark_returns.get(pd.Timestamp(exit_date), np.nan)
            if not math.isfinite(benchmark_return):
                continue
            vw_benchmark_return = vw_benchmark_returns.get(pd.Timestamp(exit_date), np.nan)
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
                    "vw_benchmark_return": vw_benchmark_return,
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
    backtest["vw_benchmark_cumulative"] = (1.0 + backtest["vw_benchmark_return"].fillna(0.0)).cumprod()
    backtest["vw_excess_return"] = backtest["strategy_return"] - backtest["vw_benchmark_return"]

    holding_detail = pd.DataFrame(holding_rows)
    metrics = compute_metrics(backtest)

    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    backtest.to_csv(BACKTEST_OUTPUT_DIR / "monthly_backtest_us.csv", index=False, encoding="utf-8-sig")
    holding_detail.to_csv(BACKTEST_OUTPUT_DIR / "monthly_holdings_us.csv", index=False, encoding="utf-8-sig")
    with (BACKTEST_OUTPUT_DIR / "performance_metrics_us.json").open("w", encoding="utf-8") as handle:
        json.dump(metrics, handle, ensure_ascii=False, indent=2)
    plot_results(backtest, metrics, BACKTEST_OUTPUT_DIR / "style_alpha_long_short_backtest_us.png")

    print("\nBacktest metrics")
    for key, value in metrics.items():
        print(f"  {key:<24} {value}")
    print(f"Total elapsed={(time.time() - total_start) / 60.0:.1f}m")
    print(f"\nSaved outputs to {BACKTEST_OUTPUT_DIR}")


if __name__ == "__main__":
    run_backtest_monthly()
