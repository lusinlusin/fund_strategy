from __future__ import annotations

import importlib.util
import json
import math
import shutil
import time
from datetime import datetime
from itertools import product
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
STRATEGY_PATH = BASE_DIR / "strategy_2026.py"
SEARCH_OUTPUT_DIR = BASE_DIR / "parameter_search_output"


# Baseline parameters copied from strategy_2026.py.
BASE_PARAMS = {
    "BACKTEST_START_DATE": "2021-01-01",
    "BACKTEST_END_DATE": "2025-12-31",
    "BACKTEST_MIN_STOCK_HOLDING": 70,
    "BACKTEST_LONG_QUANTILE": 0.10,
    "BACKTEST_SHORT_QUANTILE": 0.10,
    "BACKTEST_INCLUDE_ALPHA_BUCKET": True,
    "ROLLING_WINDOW": 245,
    "INSUFFICIENT_DATA_THRESHOLD": 245,
    "FULL_SAMPLE_FALLBACK_MAX": 300,
    "P_VALUE_THRESHOLD": 0.05,
    "SMB_STABILITY_THRESHOLD": 1.0,
    "VMG_STABILITY_THRESHOLD": 1.0,
}


# Stage 1 focuses on the start date because that had the biggest impact in your tests.
STAGE1_START_DATES = [
    "2020-01-01",
    "2021-01-01",
    "2022-01-01"
]

# Stage 2 refines a small set of parameters around the best start dates from stage 1.
STAGE2_TOP_K_START_DATES = 2
STAGE2_MIN_STOCK_HOLDINGS = [60, 65, 70]
STAGE2_QUANTILES = [0.1, 0.12, 0.15]
STAGE2_INCLUDE_ALPHA_BUCKET = [False]

# These lists are length 1 by default to keep the search manageable.
STAGE2_ROLLING_WINDOWS = [245]
STAGE2_INSUFFICIENT_THRESHOLDS = [245]
STAGE2_FALLBACK_MAX_VALUES = [300, 320, 360]
STAGE2_P_VALUE_THRESHOLDS = [0.05]
STAGE2_SMB_STABILITY_THRESHOLDS = [1.0, 1.5]
STAGE2_VMG_STABILITY_THRESHOLDS = [1.0, 1.5]


def load_strategy_module():
    spec = importlib.util.spec_from_file_location("strategy_2026_search_module", STRATEGY_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load strategy module from {STRATEGY_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_percent(value: str) -> float:
    return float(str(value).strip().replace("%", "")) / 100.0


def parse_float(value: str) -> float:
    return float(str(value).strip())


def score_metrics(metrics: dict[str, str], max_months: int) -> float:
    strategy_ann = parse_percent(metrics["Strategy Ann. Return"])
    benchmark_ann = parse_percent(metrics["Benchmark Ann. Return"])
    sharpe = parse_float(metrics["Strategy Sharpe"])
    info_ratio = parse_float(metrics["Information Ratio"])
    max_drawdown = abs(parse_percent(metrics["Strategy Max Drawdown"]))
    months = int(metrics["Rebalance Months"])
    sample_ratio = months / max_months if max_months else 0.0

    # Longer samples get a meaningful bonus, but not enough to dominate weak performance.
    return (
        strategy_ann * 100.0
        + 0.75 * (strategy_ann - benchmark_ann) * 100.0
        + 0.75 * sharpe
        + 0.50 * info_ratio
        + 2.00 * sample_ratio
        - 0.25 * max_drawdown * 100.0
    )


def build_run_name(index: int, params: dict[str, object]) -> str:
    alpha_flag = 1 if params["BACKTEST_INCLUDE_ALPHA_BUCKET"] else 0
    quantile = int(round(float(params["BACKTEST_LONG_QUANTILE"]) * 100))
    start_year = str(params["BACKTEST_START_DATE"])[:4]
    end_year = str(params["BACKTEST_END_DATE"])[:4]
    return (
        f"{index:03d}_s{start_year}_{end_year}"
        f"_h{params['BACKTEST_MIN_STOCK_HOLDING']}"
        f"_q{quantile}"
        f"_a{alpha_flag}"
        f"_rw{params['ROLLING_WINDOW']}"
        f"_fb{params['FULL_SAMPLE_FALLBACK_MAX']}"
    )


def keep_only_metrics_json(output_dir: Path):
    keep_files = {"performance_metrics_speedup.json"}
    for path in output_dir.iterdir():
        if path.name in keep_files:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def prepare_common_inputs(module):
    module.PRINT_PROGRESS = False
    fund_info, nav, factor, holding = module.load_inputs()
    nav = module.filter_nav_remove_ce_shares(nav, fund_info)
    holding = holding[["report_date"] + [col for col in holding.columns if col != "report_date" and col in nav.columns]].copy()
    nav_frame = nav.set_index("date").sort_index()
    nav_dates = list(nav_frame.index)
    return {
        "fund_info": fund_info,
        "nav": nav,
        "factor": factor,
        "holding": holding,
        "nav_frame": nav_frame,
        "nav_dates": nav_dates,
    }


def get_precompute_context(module, common_inputs, params, context_cache):
    cache_key = (
        str(params["BACKTEST_START_DATE"]),
        str(params["BACKTEST_END_DATE"]),
        int(params["ROLLING_WINDOW"]),
    )
    if cache_key in context_cache:
        return context_cache[cache_key]

    start_date = pd.Timestamp(params["BACKTEST_START_DATE"])
    end_date = pd.Timestamp(params["BACKTEST_END_DATE"])

    print(
        f"Preparing cache | start={start_date.date()} | end={end_date.date()} | "
        f"rolling_window={params['ROLLING_WINDOW']}"
    )
    stage_start = time.time()

    precompute_universe = module.build_precompute_universe(
        fund_info=common_inputs["fund_info"],
        nav=common_inputs["nav"],
        end_date=params["BACKTEST_END_DATE"],
        include_missing_setup=getattr(module, "PRECOMPUTE_INCLUDE_MISSING_SETUP", False),
    )
    full_panel = module.build_full_regression_panel(
        nav=common_inputs["nav"],
        factor=common_inputs["factor"],
        start_date=params["BACKTEST_START_DATE"],
        end_date=params["BACKTEST_END_DATE"],
        fund_codes=precompute_universe,
    )
    if full_panel.empty:
        raise ValueError("Full regression panel is empty for this start/end date.")

    panel_by_fund = {
        fund_code: frame.sort_values("date").reset_index(drop=True)
        for fund_code, frame in full_panel.groupby("fund_code", sort=True)
    }

    rebalance_dates = module.compute_month_end_rebalance_dates(
        common_inputs["nav_dates"],
        start_date=start_date,
        end_date=end_date,
    )

    signal_dates = []
    execution_dates = []
    for signal_date in rebalance_dates:
        execution_date = module.find_next_nav_date(common_inputs["nav_dates"], signal_date)
        if execution_date is None or execution_date > end_date:
            continue
        signal_dates.append(signal_date)
        execution_dates.append(execution_date)

    if len(signal_dates) < 1:
        raise ValueError("Not enough signal/execution pairs to run the backtest.")

    full_snapshot_by_signal = module.precompute_full_sample_snapshots(panel_by_fund, signal_dates)
    rolling_snapshot_by_signal = module.precompute_rolling_stability_snapshots(
        panel_by_fund,
        signal_dates,
        int(params["ROLLING_WINDOW"]),
    )

    period_boundary_dates = execution_dates + [end_date]
    nav_on_boundary_dates = common_inputs["nav_frame"].reindex(pd.to_datetime(period_boundary_dates)).ffill()
    forward_fund_returns = module.build_forward_return_frame(nav_on_boundary_dates)
    benchmark_returns = module.build_benchmark_return_series(period_boundary_dates, module.HS300_PATH, module.TREASURY_PATH)

    context = {
        "cache_key": cache_key,
        "start_date": start_date,
        "end_date": end_date,
        "precompute_universe": precompute_universe,
        "precompute_universe_set": set(precompute_universe),
        "signal_dates": signal_dates,
        "execution_dates": execution_dates,
        "full_snapshot_by_signal": full_snapshot_by_signal,
        "rolling_snapshot_by_signal": rolling_snapshot_by_signal,
        "forward_fund_returns": forward_fund_returns,
        "benchmark_returns": benchmark_returns,
    }
    context_cache[cache_key] = context

    print(
        f"Cache ready | funds={len(precompute_universe)} | signals={len(signal_dates)} | "
        f"elapsed={(time.time() - stage_start) / 60.0:.1f}m"
    )
    return context


def get_eligible_by_signal(module, common_inputs, context, min_stock_holding, eligible_cache):
    cache_key = (context["cache_key"], int(min_stock_holding))
    if cache_key in eligible_cache:
        return eligible_cache[cache_key]

    eligible_by_signal = {}
    for signal_date in context["signal_dates"]:
        eligible = module.get_eligible_funds_by_holding(
            holding=common_inputs["holding"],
            update_date=signal_date,
            min_stock_holding=min_stock_holding,
        )
        eligible_by_signal[signal_date] = [
            code for code in eligible if code in context["precompute_universe_set"]
        ]

    eligible_cache[cache_key] = eligible_by_signal
    return eligible_by_signal


def get_classified_snapshot_by_signal(module, context, params, classified_cache):
    cache_key = (
        context["cache_key"],
        int(params["INSUFFICIENT_DATA_THRESHOLD"]),
        int(params["FULL_SAMPLE_FALLBACK_MAX"]),
        float(params["P_VALUE_THRESHOLD"]),
        float(params["SMB_STABILITY_THRESHOLD"]),
        float(params["VMG_STABILITY_THRESHOLD"]),
    )
    if cache_key in classified_cache:
        return classified_cache[cache_key]

    module.INSUFFICIENT_DATA_THRESHOLD = int(params["INSUFFICIENT_DATA_THRESHOLD"])
    module.FULL_SAMPLE_FALLBACK_MAX = int(params["FULL_SAMPLE_FALLBACK_MAX"])
    module.P_VALUE_THRESHOLD = float(params["P_VALUE_THRESHOLD"])
    module.SMB_STABILITY_THRESHOLD = float(params["SMB_STABILITY_THRESHOLD"])
    module.VMG_STABILITY_THRESHOLD = float(params["VMG_STABILITY_THRESHOLD"])

    snapshot_by_signal = {}
    for signal_date in context["signal_dates"]:
        snapshot_by_signal[signal_date] = module.build_signal_snapshot(
            signal_date=signal_date,
            eligible_funds=context["precompute_universe"],
            full_snapshot_by_signal=context["full_snapshot_by_signal"],
            rolling_snapshot_by_signal=context["rolling_snapshot_by_signal"],
        )

    classified_cache[cache_key] = snapshot_by_signal
    return snapshot_by_signal


def compute_metrics_only_backtest(module, context, eligible_by_signal, params):
    backtest_rows = []
    classified_snapshot_by_signal = params["_classified_snapshot_by_signal"]
    for position, current_date in enumerate(context["signal_dates"]):
        eligible_funds = eligible_by_signal.get(current_date, [])
        snapshot = classified_snapshot_by_signal.get(current_date, pd.DataFrame()).copy()
        if eligible_funds:
            snapshot = snapshot.loc[snapshot["fund_code"].isin(eligible_funds)].copy()
        else:
            snapshot = snapshot.iloc[0:0].copy()
        holdings = module.select_portfolio_holdings(
            snapshot=snapshot,
            long_quantile=float(params["BACKTEST_LONG_QUANTILE"]),
            short_quantile=float(params["BACKTEST_SHORT_QUANTILE"]),
            include_alpha_bucket=bool(params["BACKTEST_INCLUDE_ALPHA_BUCKET"]),
        )
        if holdings.empty:
            continue

        trade_date = context["execution_dates"][position]
        exit_date = (
            context["execution_dates"][position + 1]
            if position < len(context["signal_dates"]) - 1
            else context["end_date"]
        )

        period_return_row = context["forward_fund_returns"].loc[pd.Timestamp(trade_date)]
        long_return = module.compute_side_return(holdings, period_return_row, side="long")
        short_return = module.compute_side_return(holdings, period_return_row, side="short")
        if not math.isfinite(long_return) or not math.isfinite(short_return):
            continue

        benchmark_return = float(context["benchmark_returns"].loc[pd.Timestamp(trade_date)])
        strategy_return = long_return + short_return

        backtest_rows.append(
            {
                "trade_date": trade_date,
                "holding_end_date": exit_date,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "long_return": long_return,
                "short_return": short_return,
                "long_count": int((holdings["side"] == "long").sum()),
                "short_count": int((holdings["side"] == "short").sum()),
                "style_count": int(holdings["style"].nunique()),
            }
        )

    if not backtest_rows:
        raise ValueError("The strategy did not produce any tradable monthly portfolios.")

    backtest = pd.DataFrame(backtest_rows)
    backtest["trade_date"] = pd.to_datetime(backtest["trade_date"])
    backtest["holding_end_date"] = pd.to_datetime(backtest["holding_end_date"])
    backtest["strategy_cumulative"] = (1.0 + backtest["strategy_return"]).cumprod()
    backtest["benchmark_cumulative"] = (1.0 + backtest["benchmark_return"]).cumprod()
    backtest["excess_return"] = backtest["strategy_return"] - backtest["benchmark_return"]
    return module.compute_metrics(backtest)


def run_single_backtest(
    run_index: int,
    params: dict[str, object],
    module,
    common_inputs,
    context_cache,
    eligible_cache,
    classified_cache,
) -> dict[str, object]:
    run_name = build_run_name(run_index, params)
    output_dir = SEARCH_OUTPUT_DIR / run_name
    output_dir.mkdir(parents=True, exist_ok=True)
    params_path = output_dir / "params.json"
    params_path.write_text(json.dumps(params, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    start_time = time.time()
    print(f"\nRun {run_index} start | {run_name}")
    print(params)

    try:
        context = get_precompute_context(module, common_inputs, params, context_cache)
        eligible_by_signal = get_eligible_by_signal(
            module,
            common_inputs,
            context,
            params["BACKTEST_MIN_STOCK_HOLDING"],
            eligible_cache,
        )
        params_for_run = params.copy()
        params_for_run["_classified_snapshot_by_signal"] = get_classified_snapshot_by_signal(
            module,
            context,
            params,
            classified_cache,
        )
        metrics = compute_metrics_only_backtest(module, context, eligible_by_signal, params_for_run)
    except Exception as exc:
        print(f"Run {run_index} failed | {exc}")
        return {
            "run_name": run_name,
            "output_dir": str(output_dir),
            "params": params,
            "status": "failed",
            "error": str(exc),
            "elapsed_minutes": round((time.time() - start_time) / 60.0, 2),
        }

    metrics_path = output_dir / "performance_metrics_speedup.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    elapsed_minutes = round((time.time() - start_time) / 60.0, 2)

    print(
        f"Run {run_index} done | ann={metrics['Strategy Ann. Return']} | "
        f"IR={metrics['Information Ratio']} | months={metrics['Rebalance Months']} | "
        f"elapsed={elapsed_minutes}m"
    )

    return {
        "run_name": run_name,
        "output_dir": str(output_dir),
        "params": params,
        "status": "completed",
        "metrics": metrics,
        "elapsed_minutes": elapsed_minutes,
    }


def build_stage1_configs() -> list[dict[str, object]]:
    configs = []
    for start_date in STAGE1_START_DATES:
        params = BASE_PARAMS.copy()
        params["BACKTEST_START_DATE"] = start_date
        configs.append(params)
    return configs


def build_stage2_configs(best_stage1_runs: list[dict[str, object]]) -> list[dict[str, object]]:
    configs = []
    for run in best_stage1_runs[:STAGE2_TOP_K_START_DATES]:
        start_date = run["params"]["BACKTEST_START_DATE"]
        for (
            min_stock_holding,
            quantile,
            include_alpha_bucket,
            rolling_window,
            insufficient_threshold,
            fallback_max,
            p_value_threshold,
            smb_stability_threshold,
            vmg_stability_threshold,
        ) in product(
            STAGE2_MIN_STOCK_HOLDINGS,
            STAGE2_QUANTILES,
            STAGE2_INCLUDE_ALPHA_BUCKET,
            STAGE2_ROLLING_WINDOWS,
            STAGE2_INSUFFICIENT_THRESHOLDS,
            STAGE2_FALLBACK_MAX_VALUES,
            STAGE2_P_VALUE_THRESHOLDS,
            STAGE2_SMB_STABILITY_THRESHOLDS,
            STAGE2_VMG_STABILITY_THRESHOLDS,
        ):
            if insufficient_threshold < rolling_window:
                continue
            if fallback_max < insufficient_threshold:
                continue

            params = BASE_PARAMS.copy()
            params["BACKTEST_START_DATE"] = start_date
            params["BACKTEST_MIN_STOCK_HOLDING"] = min_stock_holding
            params["BACKTEST_LONG_QUANTILE"] = quantile
            params["BACKTEST_SHORT_QUANTILE"] = quantile
            params["BACKTEST_INCLUDE_ALPHA_BUCKET"] = include_alpha_bucket
            params["ROLLING_WINDOW"] = rolling_window
            params["INSUFFICIENT_DATA_THRESHOLD"] = insufficient_threshold
            params["FULL_SAMPLE_FALLBACK_MAX"] = fallback_max
            params["P_VALUE_THRESHOLD"] = p_value_threshold
            params["SMB_STABILITY_THRESHOLD"] = smb_stability_threshold
            params["VMG_STABILITY_THRESHOLD"] = vmg_stability_threshold
            configs.append(params)
    return configs


def rank_completed_runs(results: list[dict[str, object]]) -> list[dict[str, object]]:
    completed = [result for result in results if result["status"] == "completed"]
    if not completed:
        return []

    max_months = max(int(run["metrics"]["Rebalance Months"]) for run in completed)
    for run in completed:
        run["score"] = round(score_metrics(run["metrics"], max_months=max_months), 6)

    completed.sort(key=lambda item: item["score"], reverse=True)
    return completed


def print_ranked_runs(ranked_runs: list[dict[str, object]], title: str):
    print(f"\n{title}")
    if not ranked_runs:
        print("No completed runs.")
        return

    for position, run in enumerate(ranked_runs, start=1):
        metrics = run["metrics"]
        params = run["params"]
        print(
            f"{position:02d}. {run['run_name']} | score={run['score']:.4f} | "
            f"ann={metrics['Strategy Ann. Return']} | bm={metrics['Benchmark Ann. Return']} | "
            f"sharpe={metrics['Strategy Sharpe']} | IR={metrics['Information Ratio']} | "
            f"months={metrics['Rebalance Months']} | start={params['BACKTEST_START_DATE']} | "
            f"hold={params['BACKTEST_MIN_STOCK_HOLDING']} | q={params['BACKTEST_LONG_QUANTILE']:.2f} | "
            f"alpha={params['BACKTEST_INCLUDE_ALPHA_BUCKET']}"
        )


def write_search_summary(all_results: list[dict[str, object]]):
    batch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary_rows = []
    for result in all_results:
        row = {
            "search_batch_time": batch_time,
            "run_name": result["run_name"],
            "output_dir": result["output_dir"],
            "status": result["status"],
            "elapsed_minutes": result["elapsed_minutes"],
        }
        for key, value in result["params"].items():
            row[key] = value

        if result["status"] == "completed":
            metrics = result["metrics"]
            row["score"] = result.get("score", "")
            row["Strategy Ann. Return"] = metrics.get("Strategy Ann. Return")
            row["Benchmark Ann. Return"] = metrics.get("Benchmark Ann. Return")
            row["Strategy Sharpe"] = metrics.get("Strategy Sharpe")
            row["Information Ratio"] = metrics.get("Information Ratio")
            row["Strategy Max Drawdown"] = metrics.get("Strategy Max Drawdown")
            row["Monthly Hit Rate"] = metrics.get("Monthly Hit Rate")
            row["Rebalance Months"] = metrics.get("Rebalance Months")
        else:
            row["score"] = ""
            row["error"] = result.get("error", "")

        summary_rows.append(row)

    summary = pd.DataFrame(summary_rows)
    if summary.empty:
        return

    summary_path = SEARCH_OUTPUT_DIR / "search_summary.csv"
    for attempt in range(3):
        try:
            if summary_path.exists():
                existing_summary = pd.read_csv(summary_path, encoding="utf-8-sig")
                summary_to_save = pd.concat([existing_summary, summary], ignore_index=True, sort=False)
            else:
                summary_to_save = summary
            summary_to_save.to_csv(summary_path, index=False, encoding="utf-8-sig")
            print(f"\nSaved summary to {summary_path}")
            return
        except PermissionError:
            if attempt < 2:
                print(
                    f"\nsearch_summary.csv is locked by another program. "
                    f"Retrying in 2 seconds... ({attempt + 1}/3)"
                )
                time.sleep(2)
            else:
                fallback_path = SEARCH_OUTPUT_DIR / (
                    f"search_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                )
                summary.to_csv(fallback_path, index=False, encoding="utf-8-sig")
                print(
                    f"\nsearch_summary.csv is still locked. "
                    f"Saved the current batch to {fallback_path} instead."
                )
                return


def run_parameter_search():
    SEARCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    module = load_strategy_module()
    common_inputs = prepare_common_inputs(module)
    context_cache = {}
    eligible_cache = {}
    classified_cache = {}

    all_results = []
    run_index = 1

    print("Stage 1: search start dates")
    stage1_results = []
    for params in build_stage1_configs():
        result = run_single_backtest(
            run_index,
            params,
            module,
            common_inputs,
            context_cache,
            eligible_cache,
            classified_cache,
        )
        all_results.append(result)
        stage1_results.append(result)
        run_index += 1

    ranked_stage1 = rank_completed_runs(stage1_results)
    print_ranked_runs(ranked_stage1, "Stage 1 ranking")

    if not ranked_stage1:
        print("\nNo successful stage 1 runs. Stop.")
        return

    print("\nStage 2: refine around the best start dates")
    stage2_results = []
    for params in build_stage2_configs(ranked_stage1):
        result = run_single_backtest(
            run_index,
            params,
            module,
            common_inputs,
            context_cache,
            eligible_cache,
            classified_cache,
        )
        all_results.append(result)
        stage2_results.append(result)
        run_index += 1

    ranked_all = rank_completed_runs(all_results)
    print_ranked_runs(ranked_all[:10], "Overall top 10")

    score_lookup = {run["run_name"]: run.get("score", "") for run in ranked_all}
    for result in all_results:
        if result["run_name"] in score_lookup:
            result["score"] = score_lookup[result["run_name"]]

    write_search_summary(all_results)

    if ranked_all:
        best = ranked_all[0]
        print("\nBest run")
        print(best["run_name"])
        print(best["output_dir"])
        print(best["params"])
        print(best["metrics"])


if __name__ == "__main__":
    run_parameter_search()
