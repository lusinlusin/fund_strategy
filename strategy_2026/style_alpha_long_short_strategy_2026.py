from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


if "__file__" in globals():
    BASE_DIR = Path(__file__).resolve().parent
else:
    BASE_DIR = Path.cwd()
DATA_DIR = BASE_DIR / "data"

fund_info_path = DATA_DIR / "fund_info_all.csv"
nav_path = DATA_DIR / "nav_cum.csv"
factor_path = DATA_DIR / "CH3_factors_daily_202602.xlsx"
stock_holding_path = DATA_DIR / "stock_holding_wide_2000_2025.csv"

"--------------------------------Step 1. Load and Validate Inputs--------------------------------"

# Load input data
fund_info = pd.read_csv(fund_info_path, dtype={"基金代码": str}, encoding="utf-8-sig")
nav = pd.read_csv(nav_path, encoding="utf-8-sig")
factor = pd.read_excel(factor_path)
holding = pd.read_csv(stock_holding_path, encoding="utf-8-sig")


# Basic standardization
fund_info["基金代码"] = fund_info["基金代码"].astype(str).str.zfill(6)
fund_info["成立日期"] = pd.to_datetime(fund_info["成立日期"], errors="coerce")

nav["date"] = pd.to_datetime(nav["date"], errors="coerce")
nav_fund_cols = [col for col in nav.columns if col != "date"]
nav.columns = ["date"] + [str(col).zfill(6) for col in nav_fund_cols]

factor["date"] = pd.to_datetime(factor["date"].astype(str), format="%Y%m%d", errors="coerce")

holding["report_date"] = pd.to_datetime(holding["report_date"], errors="coerce")
holding_fund_cols = [col for col in holding.columns if col != "report_date"]
holding.columns = ["report_date"] + [str(col).zfill(6) for col in holding_fund_cols]


def filter_nav_remove_ce_shares(nav, fund_info):
    nav = nav.copy()
    nav["date"] = pd.to_datetime(nav["date"], errors="coerce")

    nav_fund_cols = [col for col in nav.columns if col != "date"]
    fund_info_nav = fund_info.loc[fund_info["基金代码"].isin(nav_fund_cols)].copy()
    fund_info_nav = fund_info_nav.dropna(subset=["基金全称", "基金简称"])

    fund_info_nav["share_suffix"] = fund_info_nav["基金简称"].str.extract(r"([ACE])$", expand=False)
    fund_info_nav["short_name_base"] = fund_info_nav["基金简称"].str.replace(r"[ACE]$", "", regex=True)

    keep_codes = set(nav_fund_cols)

    grouped = fund_info_nav.groupby(["基金全称", "short_name_base"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue
        if group["share_suffix"].notna().all() and "A" in group["share_suffix"].values:
            keep_codes -= set(group["基金代码"])
            keep_codes.add(group.loc[group["share_suffix"] == "A", "基金代码"].iloc[0])

    selected_nav_cols = ["date"] + [col for col in nav.columns if col != "date" and col in keep_codes]
    return nav[selected_nav_cols].copy()


nav = filter_nav_remove_ce_shares(nav, fund_info)
holding = holding[["report_date"] + [col for col in holding.columns if col != "report_date" and col in nav.columns]].copy()


# Simple overlap check
fund_codes = set(fund_info["基金代码"].dropna())
nav_codes = {col for col in nav.columns if col != "date"}
holding_codes = {col for col in holding.columns if col != "report_date"}

common_nav_funds = sorted(fund_codes & nav_codes)
common_holding_funds = sorted(fund_codes & holding_codes)

nav_dates = nav["date"].dropna()
factor_dates = factor["date"].dropna()
overlap_dates = sorted(set(nav_dates.dt.date) & set(factor_dates.dt.date))


def filter_nav_by_holding(nav, holding, start_date, update_date, min_stock_holding=70):
    start_date = pd.Timestamp(start_date)
    update_date = pd.Timestamp(update_date)

    nav_filtered = nav.loc[(nav["date"] >= start_date) & (nav["date"] <= update_date)].copy()
    eligible_funds = get_eligible_funds_by_holding(
        holding=holding,
        update_date=update_date,
        min_stock_holding=min_stock_holding,
    )

    available_nav_funds = [col for col in nav_filtered.columns if col != "date"]
    selected_funds = [fund_code for fund_code in eligible_funds if fund_code in available_nav_funds]

    return nav_filtered[["date"] + selected_funds].copy()

# Holding distribution by year
holding_long = holding.melt(
    id_vars="report_date",
    var_name="fund_code",
    value_name="stock_holding",
)
holding_long["stock_holding"] = pd.to_numeric(holding_long["stock_holding"], errors="coerce")
holding_long = holding_long.dropna(subset=["report_date", "stock_holding"]).copy()
holding_long["year"] = holding_long["report_date"].dt.year

years = sorted(holding_long["year"].dropna().unique())
plot_dir = DATA_DIR / "graphs"
plot_dir.mkdir(parents=True, exist_ok=True)
SHOW_HOLDING_PLOTS = False
RUN_STEP2_TEST = False
RUN_STEP3_TEST = False

for page_start in range(1, len(years), 4):
    page_years = years[page_start:page_start + 4]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True)
    axes = axes.flatten()

    for ax, year in zip(axes, page_years):
        year_data = holding_long.loc[holding_long["year"] == year, "stock_holding"]
        ax.hist(year_data, bins=20, color="#4C78A8", alpha=0.8, edgecolor="white")
        ax.set_title(f"Stock Holding Distribution - {year}")
        ax.set_ylabel("Count")
        ax.set_xlabel("Stock Holding (%)")

    for ax in axes[len(page_years):]:
        ax.axis("off")

    plt.tight_layout()
    first_year = page_years[0]
    last_year = page_years[-1]
    fig.savefig(
        plot_dir / f"stock_holding_distribution_{first_year}_{last_year}.png",
        dpi=200,
        bbox_inches="tight",
    )
    if SHOW_HOLDING_PLOTS:
        plt.show()
    plt.close(fig)


# Holding bucket summary by year
holding_bucket_summary = (
    holding_long.groupby("year")["stock_holding"]
    .apply(
        lambda s: pd.Series(
            {
                "<50%": (s < 50).mean(),
                "50-60%": ((s >= 50) & (s < 60)).mean(),
                "60-70%": ((s >= 60) & (s < 70)).mean(),
                "70-80%": ((s >= 70) & (s < 80)).mean(),
                "80-90%": ((s >= 80) & (s < 90)).mean(),
                "90-100%": ((s >= 90) & (s <= 100)).mean(),
            }
        )
    )
    .unstack()
    * 100
)

print()
print("Stock holding distribution by year (%)")
print(holding_bucket_summary.round(2).to_string())


"--------------------------------Step 2. build the daily regression panel--------------------------------"
# nav_selected = filter_nav_by_holding(nav, holding, "2008-07-04", "2017-03-31", min_stock_holding=70)
# nav_selected[nav_selected["date"] == "2008-07-04"].notna().sum(axis=1)


def build_regression_panel(nav, holding, start_date, update_date, min_stock_holding, factor):
    full_panel = build_full_regression_panel(
        nav=nav,
        factor=factor,
        start_date=start_date,
        end_date=update_date,
    )
    eligible_funds = get_eligible_funds_by_holding(
        holding=holding,
        update_date=update_date,
        min_stock_holding=min_stock_holding,
    )
    regression_panel = full_panel.loc[full_panel["fund_code"].isin(eligible_funds)].copy()
    regression_panel = regression_panel.sort_values(["fund_code", "date"]).reset_index(drop=True)
    return regression_panel


def get_eligible_funds_by_holding(holding, update_date, min_stock_holding=70):
    update_date = pd.Timestamp(update_date)
    holding_filtered = holding.loc[holding["report_date"] <= update_date].copy()

    if holding_filtered.empty:
        return []

    holding_filtered = holding_filtered.sort_values("report_date")
    latest_holding = holding_filtered.tail(1)
    latest_holding_row = latest_holding.iloc[0]

    eligible_funds = []
    for fund_code in holding_filtered.columns:
        if fund_code == "report_date":
            continue
        value = latest_holding_row[fund_code]
        if pd.notna(value) and float(value) >= min_stock_holding:
            eligible_funds.append(fund_code)

    return eligible_funds


def build_full_regression_panel(nav, factor, start_date, end_date):
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)

    nav_filtered = nav.loc[(nav["date"] >= start_date) & (nav["date"] <= end_date)].copy()
    'wide table to long table'
    nav_long = nav_filtered.melt(
        id_vars="date",
        var_name="fund_code",
        value_name="nav_value",
    )
    nav_long["nav_value"] = pd.to_numeric(nav_long["nav_value"], errors="coerce")
    nav_long = nav_long.dropna(subset=["date", "nav_value"]).copy()
    nav_long = nav_long.sort_values(["fund_code", "date"]).reset_index(drop=True)

    nav_long["fund_return"] = nav_long.groupby("fund_code")["nav_value"].pct_change() # calculate fund return

    factor_panel = factor[["date", "rf_dly", "mktrf", "SMB", "VMG"]].copy()
    factor_panel = factor_panel.dropna(subset=["date"]).copy()
    factor_panel = factor_panel.sort_values("date").reset_index(drop=True)

    full_panel = nav_long.merge(factor_panel, on="date", how="inner")
    full_panel["excess_return"] = full_panel["fund_return"] - full_panel["rf_dly"]
    full_panel = full_panel.dropna(
        subset=["fund_return", "rf_dly", "mktrf", "SMB", "VMG", "excess_return"]
    ).copy()
    full_panel = full_panel.sort_values(["fund_code", "date"]).reset_index(drop=True)

    return full_panel


if RUN_STEP2_TEST:
    regression_panel_2017 = build_regression_panel(
        nav=nav,
        holding=holding,
        start_date="2008-07-04",
        update_date="2017-03-31",
        min_stock_holding=70,
        factor=factor,
    )
else:
    regression_panel_2017 = pd.DataFrame()


"--------------------------------Step 3. rolling regression and style classification--------------------------------"
import numpy as np
import statsmodels.api as sm


ROLLING_WINDOW = 245
INSUFFICIENT_DATA_THRESHOLD = 245
FULL_SAMPLE_FALLBACK_MAX = 300
P_VALUE_THRESHOLD = 0.05
SMB_STABILITY_THRESHOLD = 1.0
VMG_STABILITY_THRESHOLD = 1.0


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


def run_ols(fund_df):
    X = sm.add_constant(fund_df[["mktrf", "SMB", "VMG"]], has_constant="add")
    y = fund_df["excess_return"]
    model = sm.OLS(y, X, missing="drop").fit()
    return model


def run_rolling_style_classification(
    regression_panel,
    rolling_window=ROLLING_WINDOW,
    insufficient_data_threshold=INSUFFICIENT_DATA_THRESHOLD,
    full_sample_fallback_max=FULL_SAMPLE_FALLBACK_MAX,
    p_value_threshold=P_VALUE_THRESHOLD,
    smb_stability_threshold=SMB_STABILITY_THRESHOLD,
    vmg_stability_threshold=VMG_STABILITY_THRESHOLD,
):
    classification_rows = []
    rolling_rows = []

    fund_codes = sorted(regression_panel["fund_code"].dropna().unique())

    for fund_code in fund_codes:
        fund_df = regression_panel.loc[regression_panel["fund_code"] == fund_code].copy()
        fund_df = fund_df.sort_values("date").reset_index(drop=True)
        data_point = len(fund_df)
        fund_rolling_rows = []

        if data_point == 0:
            continue

        full_model = run_ols(fund_df)

        row = {
            "fund_code": fund_code,
            "data_point": data_point,
            "coe_alpha": full_model.params.get("const", np.nan),
            "coe_mktrf": full_model.params.get("mktrf", np.nan),
            "coe_smb": full_model.params.get("SMB", np.nan),
            "coe_vmg": full_model.params.get("VMG", np.nan),
            "p_value_alpha": full_model.pvalues.get("const", np.nan),
            "p_value_mktrf": full_model.pvalues.get("mktrf", np.nan),
            "p_value_smb": full_model.pvalues.get("SMB", np.nan),
            "p_value_vmg": full_model.pvalues.get("VMG", np.nan),
            "reg_count": 0,
            "smb_mean": np.nan,
            "smb_sd": np.nan,
            "smb_sr": np.nan,
            "vmg_mean": np.nan,
            "vmg_sd": np.nan,
            "vmg_sr": np.nan,
            "style_classification": None,
        }

        if data_point < insufficient_data_threshold:
            row["style_classification"] = "Insufficient Data"
            classification_rows.append(row)
            continue

        if data_point <= full_sample_fallback_max:
            if (
                pd.notna(row["p_value_smb"])
                and pd.notna(row["p_value_vmg"])
                and row["p_value_smb"] < p_value_threshold
                and row["p_value_vmg"] < p_value_threshold
            ):
                row["style_classification"] = classify_style(row["coe_smb"], row["coe_vmg"])
            else:
                row["style_classification"] = "Alpha"
            classification_rows.append(row)
            continue

        for end_idx in range(rolling_window, data_point + 1):
            window_df = fund_df.iloc[end_idx - rolling_window:end_idx].copy()
            rolling_model = run_ols(window_df)
            rolling_row = {
                "fund_code": fund_code,
                "date": window_df["date"].iloc[-1],
                "beta_smb": rolling_model.params.get("SMB", np.nan),
                "beta_vmg": rolling_model.params.get("VMG", np.nan),
            }
            fund_rolling_rows.append(rolling_row)
            rolling_rows.append(rolling_row)

        fund_rolling = pd.DataFrame(fund_rolling_rows)
        row["reg_count"] = len(fund_rolling)

        if len(fund_rolling) >= 2:
            row["smb_mean"] = fund_rolling["beta_smb"].mean()
            row["smb_sd"] = fund_rolling["beta_smb"].std(ddof=1)
            row["vmg_mean"] = fund_rolling["beta_vmg"].mean()
            row["vmg_sd"] = fund_rolling["beta_vmg"].std(ddof=1)

            if pd.notna(row["smb_sd"]) and row["smb_sd"] != 0:
                row["smb_sr"] = row["smb_mean"] / row["smb_sd"]
            if pd.notna(row["vmg_sd"]) and row["vmg_sd"] != 0:
                row["vmg_sr"] = row["vmg_mean"] / row["vmg_sd"]

        if (
            pd.notna(row["smb_sr"])
            and pd.notna(row["vmg_sr"])
            and abs(row["smb_sr"]) > smb_stability_threshold
            and abs(row["vmg_sr"]) > vmg_stability_threshold
        ):
            row["style_classification"] = classify_style(row["smb_sr"], row["vmg_sr"])
        else:
            row["style_classification"] = "Alpha"

        classification_rows.append(row)

    classification_result = pd.DataFrame(classification_rows)
    rolling_result = pd.DataFrame(rolling_rows)

    return classification_result, rolling_result


if RUN_STEP3_TEST and not regression_panel_2017.empty:
    classification_2017, rolling_betas_2017 = run_rolling_style_classification(
        regression_panel_2017,
        rolling_window=ROLLING_WINDOW,
        insufficient_data_threshold=INSUFFICIENT_DATA_THRESHOLD,
        full_sample_fallback_max=FULL_SAMPLE_FALLBACK_MAX,
        p_value_threshold=P_VALUE_THRESHOLD,
        smb_stability_threshold=SMB_STABILITY_THRESHOLD,
        vmg_stability_threshold=VMG_STABILITY_THRESHOLD,
    )
else:
    classification_2017 = pd.DataFrame()
    rolling_betas_2017 = pd.DataFrame()


"--------------------------------Step 4. monthly long-short backtest--------------------------------"
import bisect
import json
import math


BACKTEST_START_DATE = "2008-07-04"
BACKTEST_END_DATE = "2017-03-31"
BACKTEST_MIN_STOCK_HOLDING = 70
BACKTEST_LONG_QUANTILE = 0.10
BACKTEST_SHORT_QUANTILE = 0.10
BACKTEST_INCLUDE_ALPHA_BUCKET = True
BACKTEST_OUTPUT_DIR = BASE_DIR / "backtest_output"
HS300_PATH = DATA_DIR / "benchmark" / "sh000300.csv"
TREASURY_PATH = DATA_DIR / "benchmark" / "sh000012.csv"
PRINT_BACKTEST_PROGRESS = True


def compute_month_end_rebalance_dates_2026(nav_dates, start_date=None, end_date=None):
    month_end_dates = {}
    for current_date in nav_dates:
        if start_date is not None and current_date < start_date:
            continue
        if end_date is not None and current_date > end_date:
            continue
        month_end_dates[(current_date.year, current_date.month)] = current_date
    return [month_end_dates[key] for key in sorted(month_end_dates)]


def find_next_nav_date_2026(nav_dates, current_date):
    next_position = bisect.bisect_right(list(nav_dates), current_date)
    if next_position >= len(nav_dates):
        return None
    return nav_dates[next_position]


def load_index_close_series_2026(path):
    frame = pd.read_csv(path, parse_dates=["date"])
    series = pd.Series(frame["close"].astype(float).to_numpy(), index=frame["date"])
    return series.sort_index()


def build_forward_return_frame_2026(values_on_execution_dates):
    return values_on_execution_dates.shift(-1).divide(values_on_execution_dates).subtract(1.0)


def build_benchmark_return_series_2026(rebalance_dates, hs300_path, treasury_path):
    rebalance_index = pd.to_datetime(list(rebalance_dates))
    hs300_close = load_index_close_series_2026(hs300_path).reindex(rebalance_index, method="ffill")
    treasury_close = load_index_close_series_2026(treasury_path).reindex(rebalance_index, method="ffill")

    hs300_return = hs300_close.shift(-1).divide(hs300_close).subtract(1.0)
    treasury_return = treasury_close.shift(-1).divide(treasury_close).subtract(1.0)
    return 0.8 * hs300_return + 0.2 * treasury_return


def select_portfolio_holdings_2026(snapshot, long_quantile, short_quantile, include_alpha_bucket):
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


def compute_side_return_2026(holdings, period_return_row, side):
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


def compute_metrics_2026(backtest):
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


def plot_results_2026(backtest, metrics, save_path):
    fig = plt.figure(figsize=(14, 10), facecolor="#0f1117")
    grid = fig.add_gridspec(3, 1, hspace=0.10, height_ratios=[2, 1, 1])

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
    plt.close(fig)


def run_monthly_backtest_2026(
    nav,
    holding,
    factor,
    start_date,
    end_date,
    min_stock_holding,
    long_quantile,
    short_quantile,
    include_alpha_bucket,
    hs300_path,
    treasury_path,
):
    nav_frame = nav.set_index("date").sort_index()
    nav_dates = list(nav_frame.index)

    rebalance_dates = compute_month_end_rebalance_dates_2026(
        nav_dates,
        start_date=pd.Timestamp(start_date),
        end_date=pd.Timestamp(end_date),
    )
    if len(rebalance_dates) < 2:
        raise ValueError("Not enough month-end dates to run the backtest.")

    signal_dates = []
    execution_dates = []
    for signal_date in rebalance_dates:
        execution_date = find_next_nav_date_2026(nav_dates, signal_date)
        if execution_date is None:
            continue
        signal_dates.append(signal_date)
        execution_dates.append(execution_date)

    if len(signal_dates) < 2:
        raise ValueError("Not enough signal/execution pairs to run the backtest.")

    execution_index = pd.to_datetime(execution_dates)
    nav_on_execution = nav_frame.reindex(execution_index)
    forward_fund_returns = build_forward_return_frame_2026(nav_on_execution)
    benchmark_returns = build_benchmark_return_series_2026(
        execution_dates,
        hs300_path=hs300_path,
        treasury_path=treasury_path,
    )
    print("Building full regression panel once...")
    full_panel = build_full_regression_panel(
        nav=nav,
        factor=factor,
        start_date=start_date,
        end_date=end_date,
    )
    print("Full regression panel ready.")

    backtest_rows = []
    holding_rows = []

    for position, current_date in enumerate(signal_dates[:-1]):
        next_signal_date = signal_dates[position + 1]
        trade_date = execution_dates[position]
        exit_date = execution_dates[position + 1]

        if PRINT_BACKTEST_PROGRESS:
            print(
                f"Backtest progress: {position + 1}/{len(signal_dates) - 1} | "
                f"signal_date={pd.Timestamp(current_date).date()}"
            )

        eligible_funds = get_eligible_funds_by_holding(
            holding=holding,
            update_date=current_date,
            min_stock_holding=min_stock_holding,
        )
        regression_panel = full_panel.loc[
            (full_panel["date"] <= pd.Timestamp(current_date))
            & (full_panel["fund_code"].isin(eligible_funds))
        ].copy()
        if regression_panel.empty:
            continue

        classification_result, rolling_result = run_rolling_style_classification(
            regression_panel,
            rolling_window=ROLLING_WINDOW,
            insufficient_data_threshold=INSUFFICIENT_DATA_THRESHOLD,
            full_sample_fallback_max=FULL_SAMPLE_FALLBACK_MAX,
            p_value_threshold=P_VALUE_THRESHOLD,
            smb_stability_threshold=SMB_STABILITY_THRESHOLD,
            vmg_stability_threshold=VMG_STABILITY_THRESHOLD,
        )
        if classification_result.empty:
            continue

        snapshot = classification_result.rename(
            columns={
                "coe_alpha": "alpha",
                "style_classification": "style",
            }
        )[["fund_code", "alpha", "style"]].copy()

        holdings = select_portfolio_holdings_2026(
            snapshot=snapshot,
            long_quantile=long_quantile,
            short_quantile=short_quantile,
            include_alpha_bucket=include_alpha_bucket,
        )
        if holdings.empty:
            continue

        period_return_row = forward_fund_returns.loc[pd.Timestamp(trade_date)]
        long_return = compute_side_return_2026(holdings, period_return_row, side="long")
        short_return = compute_side_return_2026(holdings, period_return_row, side="short")
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
    return backtest, holding_detail


backtest_2026, holding_detail_2026 = run_monthly_backtest_2026(
    nav=nav,
    holding=holding,
    factor=factor,
    start_date=BACKTEST_START_DATE,
    end_date=BACKTEST_END_DATE,
    min_stock_holding=BACKTEST_MIN_STOCK_HOLDING,
    long_quantile=BACKTEST_LONG_QUANTILE,
    short_quantile=BACKTEST_SHORT_QUANTILE,
    include_alpha_bucket=BACKTEST_INCLUDE_ALPHA_BUCKET,
    hs300_path=HS300_PATH,
    treasury_path=TREASURY_PATH,
)

metrics_2026 = compute_metrics_2026(backtest_2026)
BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
backtest_2026.to_csv(BACKTEST_OUTPUT_DIR / "monthly_backtest_2026.csv", index=False, encoding="utf-8-sig")
holding_detail_2026.to_csv(BACKTEST_OUTPUT_DIR / "monthly_holdings_2026.csv", index=False, encoding="utf-8-sig")
with (BACKTEST_OUTPUT_DIR / "performance_metrics_2026.json").open("w", encoding="utf-8") as handle:
    json.dump(metrics_2026, handle, ensure_ascii=False, indent=2)
plot_results_2026(backtest_2026, metrics_2026, BACKTEST_OUTPUT_DIR / "style_alpha_long_short_backtest_2026.png")

print()
print("2026 backtest metrics")
for key, value in metrics_2026.items():
    print(f"  {key:<24} {value}")
print(f"Saved outputs to {BACKTEST_OUTPUT_DIR}")
