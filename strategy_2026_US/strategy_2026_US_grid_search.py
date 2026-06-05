"""Large cached grid search for strategy_2026_US.py.

The naive approach (call run_backtest_monthly per combination) re-loads data and
re-fits every regression for each run, which makes a 10k+ grid take days. This
engine caches the expensive, shared pieces and reuses them across combinations:

  load_inputs / return_frame         -> once
  equal/value-weight benchmarks      -> per BACKTEST_MIN_STOCK_HOLDING
  precompute universe                -> once (depends only on returns + end date)
  regression panel                   -> per FACTOR_COLUMNS
  full-sample alpha snapshots        -> per (factors, start, ALPHA_TRAILING_WINDOW)
  rolling-stability snapshots        -> per (factors, start, ROLLING_WINDOW)
  style classification               -> per (above, INSUFFICIENT/FALLBACK/P/SMB/HML)
  eligibility set                    -> per (signal_date, min_holding)

Only the cheap selection + return accounting runs per combination, and those two
hot loops are vectorized. The engine is validated against the unmodified
strategy_2026_US.run_backtest_monthly before the grid runs, so it cannot silently
drift from the main script.

Usage:
    python strategy_2026_US_grid_search.py
Outputs (parameter_search_output/):
    grid_search_summary.csv / .xlsx   one row per combination, ranked by score
"""

from __future__ import annotations

import importlib.util
import itertools
import math
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

warnings.simplefilter("ignore")

BASE_DIR = Path(__file__).resolve().parent
STRATEGY_PATH = BASE_DIR / "strategy_2026_US.py"
FACTOR_PATH = BASE_DIR / "data" / "ff5_mom_factors.csv"
SEARCH_OUTPUT_DIR = BASE_DIR / "parameter_search_output"

END_DATE = "2025-12-31"
SHORT_QUANTILE = 0          # long-only
P_VALUE_THRESHOLD = 0.05    # near-irrelevant here (see note below); fixed
SIZE_FACTOR = "smb"
VALUE_FACTOR = "hml"

# ---------------------------------------------------------------------------
# The grid. Every factor set contains mkt_rf+smb+hml (smb/hml are required by the
# style classification); the optional block {rmw, cma, mom} is fully enumerated.
#
# Note: because the regression panel runs back to fund inception (1977+), almost
# every 2010+ fund has data_point >> FULL_SAMPLE_FALLBACK_MAX, so the
# INSUFFICIENT / FALLBACK / P_VALUE knobs barely move results (classification is
# driven by the rolling-stability branch). They are fixed to keep the grid on the
# axes that actually matter.
# ---------------------------------------------------------------------------
OPTIONAL_FACTORS = ["rmw", "cma", "mom"]
FACTOR_SETS = [
    ["mkt_rf", "smb", "hml"] + list(extra)
    for r in range(len(OPTIONAL_FACTORS) + 1)
    for extra in itertools.combinations(OPTIONAL_FACTORS, r)
]  # 8 sets

GRID = {
    "FACTOR_COLUMNS": FACTOR_SETS,                       # 8
    "BACKTEST_START_DATE": ["2010-01-01", "2015-01-01", "2020-01-01"],  # 3
    "ROLLING_WINDOW": [36, 48],                          # 2 (INSUFFICIENT tied to this)
    "ALPHA_TRAILING_WINDOW": [36, 60, 120, None],        # 4
    "FULL_SAMPLE_FALLBACK_MAX": [48],                    # 1 (see note)
    "SMB_STABILITY_THRESHOLD": [1.0, 1.5, 2.0],          # 3
    "HML_STABILITY_THRESHOLD": [1.0, 1.5, 2.0],          # 3
    "BACKTEST_MIN_STOCK_HOLDING": [60, 70, 80],          # 3
    "BACKTEST_LONG_QUANTILE": [0.10, 0.20, 0.30],        # 3
    "BACKTEST_INCLUDE_ALPHA_BUCKET": [False, True],      # 2
}
# total = 8*3*2*4*1*3*3*3*3*2 = 31104

PARAM_COLUMNS = [
    "BACKTEST_START_DATE", "BACKTEST_END_DATE", "BACKTEST_MIN_STOCK_HOLDING",
    "BACKTEST_LONG_QUANTILE", "BACKTEST_SHORT_QUANTILE", "BACKTEST_INCLUDE_ALPHA_BUCKET",
    "ROLLING_WINDOW", "INSUFFICIENT_DATA_THRESHOLD", "FULL_SAMPLE_FALLBACK_MAX",
    "ALPHA_TRAILING_WINDOW", "P_VALUE_THRESHOLD", "SMB_STABILITY_THRESHOLD",
    "HML_STABILITY_THRESHOLD", "FACTOR_COLUMNS",
]
METRIC_COLUMNS = [
    "Strategy Ann. Return", "Benchmark (EW) Ann. Return", "Benchmark (VW) Ann. Return",
    "Strategy Sharpe", "Benchmark (EW) Sharpe", "Benchmark (VW) Sharpe",
    "Information Ratio (vs EW)", "Information Ratio (vs VW)", "Strategy Max Drawdown",
    "Monthly Hit Rate (vs EW)", "Return Months", "Rebalance Years",
]


def load_strategy_module():
    spec = importlib.util.spec_from_file_location("strategy_2026_us_grid_module", STRATEGY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Vectorized helpers (validated against the module below)
# ---------------------------------------------------------------------------

def _style_from_signals(size_arr, value_arr):
    style = np.full(len(size_arr), "Alpha", dtype=object)
    style[(size_arr < 0) & (value_arr < 0)] = "Large Growth"
    style[(size_arr < 0) & (value_arr > 0)] = "Large Value"
    style[(size_arr > 0) & (value_arr < 0)] = "Small Growth"
    style[(size_arr > 0) & (value_arr > 0)] = "Small Value"
    return style


def classify_vectorized(merged, insuff, fallback, pthr, size_st, value_st):
    """Vectorized equivalent of strategy_2026_US.build_signal_snapshot's style logic."""
    dp = merged["data_point"].to_numpy(dtype=float)
    coe_size = merged[f"coe_{SIZE_FACTOR}"].to_numpy(dtype=float)
    coe_value = merged[f"coe_{VALUE_FACTOR}"].to_numpy(dtype=float)
    p_size = merged[f"p_value_{SIZE_FACTOR}"].to_numpy(dtype=float)
    p_value = merged[f"p_value_{VALUE_FACTOR}"].to_numpy(dtype=float)
    size_sr = merged["size_sr"].to_numpy(dtype=float)
    value_sr = merged["value_sr"].to_numpy(dtype=float)

    fb_sig = (~np.isnan(p_size)) & (~np.isnan(p_value)) & (p_size < pthr) & (p_value < pthr)
    fb_style = np.where(fb_sig, _style_from_signals(coe_size, coe_value), "Alpha")

    rl_sig = (
        (~np.isnan(size_sr)) & (~np.isnan(value_sr))
        & (np.abs(size_sr) > size_st) & (np.abs(value_sr) > value_st)
    )
    rl_style = np.where(rl_sig, _style_from_signals(size_sr, value_sr), "Alpha")

    style = np.empty(len(merged), dtype=object)
    insuff_mask = dp < insuff
    fallback_mask = (~insuff_mask) & (dp <= fallback)
    rolling_mask = (~insuff_mask) & (dp > fallback)
    style[insuff_mask] = "Insufficient Data"
    style[fallback_mask] = fb_style[fallback_mask]
    style[rolling_mask] = rl_style[rolling_mask]
    return style


def long_only_period_returns(holdings, return_frame, holding_dates):
    """Equal-(bucket)-weight long return per holding month, dropping NaN funds and
    renormalizing over survivors -- identical to compute_side_return(side='long')."""
    codes = holdings["fund_code"].tolist()
    weights = holdings["weight"].to_numpy(dtype=float)
    sub = return_frame.reindex(index=pd.to_datetime(list(holding_dates)), columns=codes)
    R = sub.to_numpy(dtype=float)
    mask = ~np.isnan(R)
    WW = np.broadcast_to(weights, R.shape)
    num = np.where(mask, R * WW, 0.0).sum(axis=1)
    den = np.where(mask, WW, 0.0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return num / den  # nan where den == 0


# ---------------------------------------------------------------------------
# Cached search engine
# ---------------------------------------------------------------------------

class GridEngine:
    def __init__(self, module):
        self.m = module
        module.PRINT_PROGRESS = False
        module.SHORT_QUANTILE = SHORT_QUANTILE
        module.P_VALUE_THRESHOLD = P_VALUE_THRESHOLD
        module.SIZE_FACTOR = SIZE_FACTOR
        module.VALUE_FACTOR = VALUE_FACTOR

        module.FACTOR_COLUMNS = ["mkt_rf", "smb", "hml", "rmw", "cma", "mom"]
        self.returns, _ = module.load_inputs()

        raw = pd.read_csv(FACTOR_PATH, parse_dates=["date"])
        raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
        for col in raw.columns:
            if col != "date":
                raw[col] = pd.to_numeric(raw[col], errors="coerce")
        self.factor_raw = raw

        self.return_frame = (
            self.returns.pivot_table(index="date", columns="fund_code", values="fund_return", aggfunc="last")
            .sort_index()
        )
        self.monthly_dates = list(self.return_frame.index)
        self.precompute_universe = module.build_precompute_universe(self.returns, END_DATE)
        self.precompute_universe_set = set(self.precompute_universe)

        self._bench = {}        # min_holding -> (ew, vw)
        self._panel = {}        # factors -> panel_by_fund
        self._signals = {}      # start -> (signal_dates, holding_schedule)
        self._full = {}         # (factors, start, alpha_window) -> snapshot dict
        self._rolling = {}      # (factors, start, rolling_window) -> snapshot dict
        self._merged = {}       # (factors, start, aw, rw) -> {signal: merged df}
        self._styles = {}       # (merged_key, insuff, fb, smb_st, hml_st) -> {signal: style array}
        self._eligible = {}     # (signal, min_holding) -> set

    def reset_heavy_caches(self):
        # Called when the factor set changes; keeps factor-independent caches
        # (benchmarks, eligibility, signals) and frees the per-factor snapshots.
        self._panel.clear()
        self._full.clear()
        self._rolling.clear()
        self._merged.clear()
        self._styles.clear()

    def factor_frame(self, factors):
        return self.factor_raw.dropna(subset=["date", "rf"] + list(factors)).sort_values("date").reset_index(drop=True)

    def benchmarks(self, min_holding):
        if min_holding not in self._bench:
            ew = self.m.build_equal_weight_benchmark_series(self.returns, min_holding)
            vw = self.m.build_value_weight_benchmark_series(self.returns, min_holding)
            self._bench[min_holding] = (ew, vw)
        return self._bench[min_holding]

    def panel(self, factors):
        key = tuple(factors)
        if key not in self._panel:
            self.m.FACTOR_COLUMNS = list(factors)
            full_panel = self.m.build_full_regression_panel(
                self.returns, self.factor_frame(factors), END_DATE, self.precompute_universe
            )
            self._panel[key] = {
                code: frame.sort_values("date").reset_index(drop=True)
                for code, frame in full_panel.groupby("fund_code", sort=True)
            }
        return self._panel[key]

    def signals(self, start):
        if start not in self._signals:
            signal_dates = self.m.compute_year_end_rebalance_dates(
                self.monthly_dates, start_date=pd.Timestamp(start), end_date=pd.Timestamp(END_DATE)
            )
            schedule = self.m.build_annual_holding_schedule(self.monthly_dates, signal_dates, pd.Timestamp(END_DATE))
            signal_dates = [s for s in signal_dates if s in schedule]
            self._signals[start] = (signal_dates, schedule)
        return self._signals[start]

    def merged(self, factors, start, alpha_window, rolling_window):
        key = (tuple(factors), start, alpha_window, rolling_window)
        if key in self._merged:
            return key
        self.m.FACTOR_COLUMNS = list(factors)
        panel = self.panel(factors)
        signal_dates, _ = self.signals(start)

        fkey = (tuple(factors), start, alpha_window)
        if fkey not in self._full:
            self._full[fkey] = self.m.precompute_full_sample_snapshots(panel, signal_dates, alpha_window)
        rkey = (tuple(factors), start, rolling_window)
        if rkey not in self._rolling:
            self._rolling[rkey] = self.m.precompute_rolling_stability_snapshots(panel, signal_dates, rolling_window)

        full_by_signal = self._full[fkey]
        rolling_by_signal = self._rolling[rkey]
        merged_by_signal = {}
        for signal in signal_dates:
            full = full_by_signal.get(pd.Timestamp(signal), pd.DataFrame())
            roll = rolling_by_signal.get(pd.Timestamp(signal), pd.DataFrame())
            if full.empty:
                continue
            merged_by_signal[signal] = full.merge(roll, on="fund_code", how="left")
        self._merged[key] = merged_by_signal
        return key

    def styles(self, merged_key, insuff, fallback, smb_st, hml_st):
        skey = (merged_key, insuff, fallback, smb_st, hml_st)
        if skey not in self._styles:
            merged_by_signal = self._merged[merged_key]
            out = {}
            for signal, merged in merged_by_signal.items():
                styled = merged.copy()
                styled["style"] = classify_vectorized(merged, insuff, fallback, P_VALUE_THRESHOLD, smb_st, hml_st)
                styled["alpha"] = styled["coe_alpha"]
                out[signal] = styled
            self._styles[skey] = out
        return self._styles[skey]

    def eligible(self, signal, min_holding):
        key = (signal, min_holding)
        if key not in self._eligible:
            elig = self.m.get_eligible_funds_by_signal(self.returns, signal, min_holding)
            self._eligible[key] = self.precompute_universe_set.intersection(elig)
        return self._eligible[key]

    def run(self, params):
        factors = params["FACTOR_COLUMNS"]
        start = params["BACKTEST_START_DATE"]
        rolling_window = params["ROLLING_WINDOW"]
        alpha_window = params["ALPHA_TRAILING_WINDOW"]
        insuff = params["INSUFFICIENT_DATA_THRESHOLD"]
        fallback = params["FULL_SAMPLE_FALLBACK_MAX"]
        smb_st = params["SMB_STABILITY_THRESHOLD"]
        hml_st = params["HML_STABILITY_THRESHOLD"]
        min_holding = params["BACKTEST_MIN_STOCK_HOLDING"]
        long_quantile = params["BACKTEST_LONG_QUANTILE"]
        include_alpha = params["BACKTEST_INCLUDE_ALPHA_BUCKET"]

        merged_key = self.merged(factors, start, alpha_window, rolling_window)
        styles_by_signal = self.styles(merged_key, insuff, fallback, smb_st, hml_st)
        signal_dates, schedule = self.signals(start)
        ew, vw = self.benchmarks(min_holding)

        sr_all, br_all, vw_all, reb_all, end_all = [], [], [], [], []
        for signal in signal_dates:
            styled = styles_by_signal.get(signal)
            if styled is None:
                continue
            eligible_set = self.eligible(signal, min_holding)
            snapshot = styled.loc[styled["fund_code"].isin(eligible_set), ["fund_code", "alpha", "style"]]
            if snapshot.empty:
                continue
            holdings = self.m.select_portfolio_holdings(snapshot, long_quantile, SHORT_QUANTILE, include_alpha)
            if holdings.empty:
                continue

            holding_dates = schedule[signal]["holding_dates"]
            month_ret = long_only_period_returns(holdings, self.return_frame, holding_dates)
            ew_ret = ew.reindex(pd.to_datetime(list(holding_dates))).to_numpy(dtype=float)
            vw_ret = vw.reindex(pd.to_datetime(list(holding_dates))).to_numpy(dtype=float)

            keep = np.isfinite(month_ret) & np.isfinite(ew_ret)  # mirrors the main-script skip rule
            for i, hold_date in enumerate(holding_dates):
                if not keep[i]:
                    continue
                sr_all.append(float(month_ret[i]))
                br_all.append(float(ew_ret[i]))
                vw_all.append(float(vw_ret[i]))
                reb_all.append(signal)
                end_all.append(hold_date)

        if not sr_all:
            return None

        bt = pd.DataFrame({
            "strategy_return": sr_all,
            "benchmark_return": br_all,
            "vw_benchmark_return": vw_all,
            "rebalance_date": pd.to_datetime(reb_all),
            "holding_end_date": pd.to_datetime(end_all),
        })
        bt["strategy_cumulative"] = (1.0 + bt["strategy_return"]).cumprod()
        bt["benchmark_cumulative"] = (1.0 + bt["benchmark_return"]).cumprod()
        bt["excess_return"] = bt["strategy_return"] - bt["benchmark_return"]
        bt["vw_benchmark_cumulative"] = (1.0 + bt["vw_benchmark_return"].fillna(0.0)).cumprod()
        bt["vw_excess_return"] = bt["strategy_return"] - bt["vw_benchmark_return"]
        return self.m.compute_metrics(bt)


# ---------------------------------------------------------------------------
# Scoring, config expansion, summary
# ---------------------------------------------------------------------------

def parse_percent(value):
    return float(str(value).strip().replace("%", "")) / 100.0


def safe_float(value):
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def score_metrics(metrics, max_months):
    strat = parse_percent(metrics["Strategy Ann. Return"])
    bench = parse_percent(metrics["Benchmark (EW) Ann. Return"])
    sharpe = safe_float(metrics["Strategy Sharpe"])
    ir = safe_float(metrics["Information Ratio (vs EW)"])
    mdd = abs(parse_percent(metrics["Strategy Max Drawdown"]))
    months = int(metrics["Return Months"])
    sample_ratio = months / max_months if max_months else 0.0
    return (
        strat * 100.0
        + 0.75 * (strat - bench) * 100.0
        + 0.75 * sharpe
        + 0.50 * ir
        + 2.00 * sample_ratio
        - 0.25 * mdd * 100.0
    )


def expand_configs():
    keys = list(GRID.keys())
    configs = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        params = dict(zip(keys, combo))
        params["BACKTEST_END_DATE"] = END_DATE
        params["BACKTEST_SHORT_QUANTILE"] = SHORT_QUANTILE
        params["P_VALUE_THRESHOLD"] = P_VALUE_THRESHOLD
        params["INSUFFICIENT_DATA_THRESHOLD"] = params["ROLLING_WINDOW"]
        configs.append(params)
    return configs


def validate_engine(engine, module):
    """Confirm the cached engine reproduces run_backtest_monthly exactly."""
    print("Validating engine against run_backtest_monthly ...")
    checks = [
        {"FACTOR_COLUMNS": ["mkt_rf", "smb", "hml"], "BACKTEST_START_DATE": "2010-01-01",
         "ROLLING_WINDOW": 36, "ALPHA_TRAILING_WINDOW": 60, "FULL_SAMPLE_FALLBACK_MAX": 48,
         "SMB_STABILITY_THRESHOLD": 1.5, "HML_STABILITY_THRESHOLD": 1.5,
         "BACKTEST_MIN_STOCK_HOLDING": 70, "BACKTEST_LONG_QUANTILE": 0.10, "BACKTEST_INCLUDE_ALPHA_BUCKET": False},
        {"FACTOR_COLUMNS": ["mkt_rf", "smb", "hml", "mom"], "BACKTEST_START_DATE": "2015-01-01",
         "ROLLING_WINDOW": 48, "ALPHA_TRAILING_WINDOW": None, "FULL_SAMPLE_FALLBACK_MAX": 48,
         "SMB_STABILITY_THRESHOLD": 1.0, "HML_STABILITY_THRESHOLD": 2.0,
         "BACKTEST_MIN_STOCK_HOLDING": 60, "BACKTEST_LONG_QUANTILE": 0.20, "BACKTEST_INCLUDE_ALPHA_BUCKET": True},
    ]
    ok = True
    for check in checks:
        params = dict(check)
        params["BACKTEST_END_DATE"] = END_DATE
        params["BACKTEST_SHORT_QUANTILE"] = SHORT_QUANTILE
        params["P_VALUE_THRESHOLD"] = P_VALUE_THRESHOLD
        params["INSUFFICIENT_DATA_THRESHOLD"] = params["ROLLING_WINDOW"]
        engine_metrics = engine.run(params)

        for key, value in params.items():
            setattr(module, key, value)
        module.SIZE_FACTOR, module.VALUE_FACTOR = SIZE_FACTOR, VALUE_FACTOR
        module.PRINT_PROGRESS = False
        scratch = SEARCH_OUTPUT_DIR / "_validation"
        module.BACKTEST_OUTPUT_DIR = scratch
        module.run_backtest_monthly()
        import json
        ref = json.loads((scratch / "performance_metrics_us.json").read_text())

        for metric in ["Strategy Ann. Return", "Information Ratio (vs EW)", "Strategy Sharpe",
                       "Strategy Max Drawdown", "Return Months", "Benchmark (VW) Ann. Return"]:
            if str(engine_metrics[metric]) != str(ref[metric]):
                ok = False
                print(f"  MISMATCH {metric}: engine={engine_metrics[metric]} ref={ref[metric]} | {check}")
        print(f"  check ok={engine_metrics is not None} | ann engine={engine_metrics['Strategy Ann. Return']} ref={ref['Strategy Ann. Return']}")
    if scratch_exists := (SEARCH_OUTPUT_DIR / "_validation").exists():
        import shutil
        shutil.rmtree(SEARCH_OUTPUT_DIR / "_validation")
    print(f"Validation {'PASSED' if ok else 'FAILED'}\n")
    return ok


def main():
    SEARCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    module = load_strategy_module()
    engine = GridEngine(module)

    if not validate_engine(engine, module):
        raise SystemExit("Engine validation failed; aborting before the grid.")

    configs = expand_configs()
    total = len(configs)
    print(f"Running grid: {total} combinations")
    start_time = time.time()

    rows = []
    prev_factors = None
    for index, params in enumerate(configs, start=1):
        if prev_factors is not None and tuple(params["FACTOR_COLUMNS"]) != prev_factors:
            engine.reset_heavy_caches()
        prev_factors = tuple(params["FACTOR_COLUMNS"])
        try:
            metrics = engine.run(params)
            status = "completed" if metrics else "empty"
        except Exception as exc:  # noqa: BLE001
            metrics, status = None, f"failed: {exc}"
        rows.append({"params": params, "metrics": metrics, "status": status})
        if index % 1000 == 0 or index == total:
            print(f"  {index}/{total} | elapsed={(time.time() - start_time) / 60.0:.1f}m")

    completed = [r for r in rows if r["metrics"]]
    max_months = max((int(r["metrics"]["Return Months"]) for r in completed), default=0)
    for r in completed:
        r["score"] = round(score_metrics(r["metrics"], max_months), 6)

    summary_rows = []
    for r in rows:
        row = {"status": r["status"]}
        for key in PARAM_COLUMNS:
            value = r["params"].get(key)
            row[key] = ", ".join(value) if isinstance(value, list) else value
        if r["metrics"]:
            row["score"] = r.get("score", "")
            for key in METRIC_COLUMNS:
                row[key] = r["metrics"].get(key)
        summary_rows.append(row)

    column_order = ["status"] + PARAM_COLUMNS + ["score"] + METRIC_COLUMNS
    summary = pd.DataFrame(summary_rows)
    for col in column_order:
        if col not in summary.columns:
            summary[col] = ""
    summary = summary[column_order]
    summary["search_batch_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = summary.sort_values("score", ascending=False, na_position="last").reset_index(drop=True)
    summary.insert(0, "rank", range(1, len(summary) + 1))

    csv_path = SEARCH_OUTPUT_DIR / "grid_search_summary.csv"
    xlsx_path = SEARCH_OUTPUT_DIR / "grid_search_summary.xlsx"
    summary.to_csv(csv_path, index=False, encoding="utf-8-sig")
    summary.to_excel(xlsx_path, index=False, sheet_name="grid_search")

    print(f"\nDone: {len(completed)}/{total} completed in {(time.time() - start_time) / 60.0:.1f}m")
    print(f"Saved:\n  {csv_path}\n  {xlsx_path}\n")
    show = ["rank", "score", "FACTOR_COLUMNS", "BACKTEST_START_DATE", "ALPHA_TRAILING_WINDOW",
            "BACKTEST_LONG_QUANTILE", "SMB_STABILITY_THRESHOLD", "HML_STABILITY_THRESHOLD",
            "BACKTEST_MIN_STOCK_HOLDING", "BACKTEST_INCLUDE_ALPHA_BUCKET",
            "Strategy Ann. Return", "Benchmark (EW) Ann. Return", "Benchmark (VW) Ann. Return",
            "Information Ratio (vs EW)", "Information Ratio (vs VW)", "Strategy Sharpe"]
    print("Top 15 by score:")
    print(summary[show].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
