"""Parameter-tuning driver for strategy_2026_US.py.

It varies the strategy parameters, runs the full US backtest for each combination
by reusing the exact functions/globals in strategy_2026_US.py (so results never
drift from the main script), collects each run's "Backtest metrics", and writes a
single combined table to both CSV and Excel. The layout mirrors the China
reference file strategy_2026/parameter_search_output/search_summary.csv.

Two search modes (set SEARCH_MODE):
  - "ofat": vary one parameter at a time around BASE_PARAMS (matches "分别改变参数").
  - "grid": full Cartesian product of every list in SEARCH_GRID.

Usage:
    python strategy_2026_US_parameter_search.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import shutil
import time
import warnings
from datetime import datetime
from itertools import product
from pathlib import Path

import pandas as pd

warnings.simplefilter("ignore")

BASE_DIR = Path(__file__).resolve().parent
STRATEGY_PATH = BASE_DIR / "strategy_2026_US.py"
SEARCH_OUTPUT_DIR = BASE_DIR / "parameter_search_output"


# ---------------------------------------------------------------------------
# What to search
# ---------------------------------------------------------------------------

# Baseline = current configuration in strategy_2026_US.py. Every "ofat" run starts
# from this and changes a single parameter; "grid" ignores the swept keys below.
BASE_PARAMS = {
    "BACKTEST_START_DATE": "2010-01-01",
    "BACKTEST_END_DATE": "2025-12-31",
    "BACKTEST_MIN_STOCK_HOLDING": 70,
    "BACKTEST_LONG_QUANTILE": 0.10,
    "BACKTEST_SHORT_QUANTILE": 0,
    "BACKTEST_INCLUDE_ALPHA_BUCKET": False,
    "ROLLING_WINDOW": 36,
    "INSUFFICIENT_DATA_THRESHOLD": 36,
    "FULL_SAMPLE_FALLBACK_MAX": 48,
    "ALPHA_TRAILING_WINDOW": 60,
    "P_VALUE_THRESHOLD": 0.05,
    "SMB_STABILITY_THRESHOLD": 1.5,
    "HML_STABILITY_THRESHOLD": 1.5,
    "FACTOR_COLUMNS": ["mkt_rf", "smb", "hml"],
}

SEARCH_MODE = "ofat"  # "ofat" or "grid"

# Values to try for each parameter. In "ofat" mode each list is explored
# independently around BASE_PARAMS; in "grid" mode the full product is run.
SEARCH_GRID = {
    "BACKTEST_START_DATE": ["2010-01-01", "2015-01-01", "2020-01-01"],
    "BACKTEST_MIN_STOCK_HOLDING": [60, 70, 80],
    "BACKTEST_LONG_QUANTILE": [0.10, 0.20, 0.30],
    "BACKTEST_INCLUDE_ALPHA_BUCKET": [False, True],
    "ALPHA_TRAILING_WINDOW": [36, 60, 120, None],
    "ROLLING_WINDOW": [36, 48],
    "FULL_SAMPLE_FALLBACK_MAX": [48, 60],
    "SMB_STABILITY_THRESHOLD": [1.0, 1.5, 2.0],
    "HML_STABILITY_THRESHOLD": [1.0, 1.5, 2.0],
    "FACTOR_COLUMNS": [
        ["mkt_rf", "smb", "hml"],
        ["mkt_rf", "smb", "hml", "mom"],
        ["mkt_rf", "smb", "hml", "rmw", "cma"],
        ["mkt_rf", "smb", "hml", "rmw", "cma", "mom"],
    ],
}

# Parameters written to the summary, in order (mirrors the reference file's layout
# but adapted to the US strategy: FACTOR set + alpha window, no VMG).
PARAM_COLUMNS = [
    "BACKTEST_START_DATE",
    "BACKTEST_END_DATE",
    "BACKTEST_MIN_STOCK_HOLDING",
    "BACKTEST_LONG_QUANTILE",
    "BACKTEST_SHORT_QUANTILE",
    "BACKTEST_INCLUDE_ALPHA_BUCKET",
    "ROLLING_WINDOW",
    "INSUFFICIENT_DATA_THRESHOLD",
    "FULL_SAMPLE_FALLBACK_MAX",
    "ALPHA_TRAILING_WINDOW",
    "P_VALUE_THRESHOLD",
    "SMB_STABILITY_THRESHOLD",
    "HML_STABILITY_THRESHOLD",
    "FACTOR_COLUMNS",
]

# Metric columns harvested from each run (keys as emitted by compute_metrics).
METRIC_COLUMNS = [
    "Strategy Ann. Return",
    "Benchmark (EW) Ann. Return",
    "Benchmark (VW) Ann. Return",
    "Strategy Sharpe",
    "Benchmark (EW) Sharpe",
    "Benchmark (VW) Sharpe",
    "Information Ratio (vs EW)",
    "Information Ratio (vs VW)",
    "Strategy Max Drawdown",
    "Monthly Hit Rate (vs EW)",
    "Return Months",
    "Rebalance Years",
]

FACTOR_CODE = {"mkt_rf": "M", "smb": "S", "hml": "H", "rmw": "R", "cma": "C", "mom": "U"}


# ---------------------------------------------------------------------------
# Module loading / data caching
# ---------------------------------------------------------------------------

def load_strategy_module():
    spec = importlib.util.spec_from_file_location("strategy_2026_us_search_module", STRATEGY_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load strategy module from {STRATEGY_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_input_cache(module):
    """Replace load_inputs with a memoized version so the 58 MB returns file is
    parsed once per distinct factor set instead of once per run."""
    original_load_inputs = module.load_inputs
    cache: dict[tuple, tuple] = {}

    def cached_load_inputs():
        key = tuple(module.FACTOR_COLUMNS)
        if key not in cache:
            cache[key] = original_load_inputs()
        return cache[key]

    module.load_inputs = cached_load_inputs


# ---------------------------------------------------------------------------
# Scoring (same shape as the China search; scored against the EW benchmark)
# ---------------------------------------------------------------------------

def parse_percent(value) -> float:
    return float(str(value).strip().replace("%", "")) / 100.0


def parse_float(value) -> float:
    return float(str(value).strip())


def safe_float(value) -> float:
    try:
        result = parse_float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def score_metrics(metrics: dict, max_months: int) -> float:
    strategy_ann = parse_percent(metrics["Strategy Ann. Return"])
    benchmark_ann = parse_percent(metrics["Benchmark (EW) Ann. Return"])
    sharpe = safe_float(metrics["Strategy Sharpe"])
    info_ratio = safe_float(metrics["Information Ratio (vs EW)"])
    max_drawdown = abs(parse_percent(metrics["Strategy Max Drawdown"]))
    months = int(metrics["Return Months"])
    sample_ratio = months / max_months if max_months else 0.0

    return (
        strategy_ann * 100.0
        + 0.75 * (strategy_ann - benchmark_ann) * 100.0
        + 0.75 * sharpe
        + 0.50 * info_ratio
        + 2.00 * sample_ratio
        - 0.25 * max_drawdown * 100.0
    )


# ---------------------------------------------------------------------------
# Running one configuration
# ---------------------------------------------------------------------------

def build_run_name(index: int, params: dict) -> str:
    alpha_flag = 1 if params["BACKTEST_INCLUDE_ALPHA_BUCKET"] else 0
    quantile = int(round(float(params["BACKTEST_LONG_QUANTILE"]) * 100))
    start_year = str(params["BACKTEST_START_DATE"])[:4]
    end_year = str(params["BACKTEST_END_DATE"])[:4]
    alpha_window = params["ALPHA_TRAILING_WINDOW"]
    alpha_window_code = "exp" if alpha_window is None else str(alpha_window)
    factor_code = "".join(FACTOR_CODE.get(name, "?") for name in params["FACTOR_COLUMNS"])
    return (
        f"{index:03d}_s{start_year}_{end_year}"
        f"_h{params['BACKTEST_MIN_STOCK_HOLDING']}"
        f"_q{quantile}"
        f"_a{alpha_flag}"
        f"_rw{params['ROLLING_WINDOW']}"
        f"_fb{params['FULL_SAMPLE_FALLBACK_MAX']}"
        f"_aw{alpha_window_code}"
        f"_f{factor_code}"
    )


def apply_params(module, params: dict):
    for key, value in params.items():
        setattr(module, key, value)


def run_single_backtest(run_index: int, params: dict, module) -> dict:
    run_name = build_run_name(run_index, params)
    output_dir = SEARCH_OUTPUT_DIR / run_name
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    apply_params(module, params)
    module.BACKTEST_OUTPUT_DIR = output_dir
    module.PRINT_PROGRESS = False

    start_time = time.time()
    print(f"\nRun {run_index} | {run_name}")
    try:
        module.run_backtest_monthly()
        metrics = json.loads((output_dir / "performance_metrics_us.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - record and continue the sweep
        print(f"  failed: {exc}")
        return {
            "run_name": run_name,
            "output_dir": str(output_dir),
            "status": "failed",
            "error": str(exc),
            "elapsed_minutes": round((time.time() - start_time) / 60.0, 2),
            "params": params,
        }

    elapsed_minutes = round((time.time() - start_time) / 60.0, 2)
    keep_only_metrics_json(output_dir)
    print(
        f"  ann={metrics['Strategy Ann. Return']} | "
        f"IR(EW)={metrics['Information Ratio (vs EW)']} | "
        f"months={metrics['Return Months']} | elapsed={elapsed_minutes}m"
    )
    return {
        "run_name": run_name,
        "output_dir": str(output_dir),
        "status": "completed",
        "elapsed_minutes": elapsed_minutes,
        "params": params,
        "metrics": metrics,
    }


def keep_only_metrics_json(output_dir: Path):
    keep_files = {"performance_metrics_us.json", "params.json"}
    for path in output_dir.iterdir():
        if path.name in keep_files:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


# ---------------------------------------------------------------------------
# Building the list of configurations
# ---------------------------------------------------------------------------

def build_ofat_configs() -> list[dict]:
    """Baseline plus one run per swept value, changing a single key at a time."""
    configs = [BASE_PARAMS.copy()]
    seen = {json.dumps(BASE_PARAMS, sort_keys=True, default=str)}
    for key, values in SEARCH_GRID.items():
        for value in values:
            params = BASE_PARAMS.copy()
            params[key] = value
            if key == "ROLLING_WINDOW":
                # Keep the insufficient-data gate aligned with the rolling window.
                params["INSUFFICIENT_DATA_THRESHOLD"] = value
            signature = json.dumps(params, sort_keys=True, default=str)
            if signature in seen:
                continue
            seen.add(signature)
            configs.append(params)
    return configs


def build_grid_configs() -> list[dict]:
    keys = list(SEARCH_GRID.keys())
    configs = []
    for combo in product(*(SEARCH_GRID[key] for key in keys)):
        params = BASE_PARAMS.copy()
        params.update(dict(zip(keys, combo)))
        if "ROLLING_WINDOW" in SEARCH_GRID:
            params["INSUFFICIENT_DATA_THRESHOLD"] = params["ROLLING_WINDOW"]
        configs.append(params)
    return configs


# ---------------------------------------------------------------------------
# Writing the combined summary table (CSV + Excel)
# ---------------------------------------------------------------------------

def build_summary_frame(results: list[dict], batch_time: str) -> pd.DataFrame:
    completed = [r for r in results if r["status"] == "completed"]
    max_months = max((int(r["metrics"]["Return Months"]) for r in completed), default=0)
    for run in completed:
        run["score"] = round(score_metrics(run["metrics"], max_months), 6)

    rows = []
    for result in results:
        row = {
            "run_name": result["run_name"],
            "output_dir": result["output_dir"],
            "status": result["status"],
            "elapsed_minutes": result["elapsed_minutes"],
        }
        for key in PARAM_COLUMNS:
            value = result["params"].get(key)
            row[key] = ", ".join(value) if isinstance(value, list) else value
        if result["status"] == "completed":
            row["score"] = result.get("score", "")
            for key in METRIC_COLUMNS:
                row[key] = result["metrics"].get(key)
        else:
            row["score"] = ""
            row["error"] = result.get("error", "")
        row["search_batch_time"] = batch_time
        rows.append(row)

    column_order = (
        ["run_name", "output_dir", "status", "elapsed_minutes"]
        + PARAM_COLUMNS
        + ["score"]
        + METRIC_COLUMNS
        + ["search_batch_time"]
    )
    frame = pd.DataFrame(rows)
    for column in column_order:
        if column not in frame.columns:
            frame[column] = ""
    extra = [c for c in frame.columns if c not in column_order]
    return frame[column_order + extra]


def write_summary(summary: pd.DataFrame):
    SEARCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = SEARCH_OUTPUT_DIR / "search_summary.csv"
    if csv_path.exists():
        existing = pd.read_csv(csv_path, encoding="utf-8-sig")
        combined = pd.concat([existing, summary], ignore_index=True, sort=False)
    else:
        combined = summary
    combined.to_csv(csv_path, index=False, encoding="utf-8-sig")

    xlsx_path = SEARCH_OUTPUT_DIR / "search_summary.xlsx"
    combined.to_excel(xlsx_path, index=False, sheet_name="search_summary")
    print(f"\nSaved summary:\n  {csv_path}\n  {xlsx_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_parameter_search():
    module = load_strategy_module()
    install_input_cache(module)

    if SEARCH_MODE == "grid":
        configs = build_grid_configs()
    else:
        configs = build_ofat_configs()

    print(f"Search mode: {SEARCH_MODE} | total runs: {len(configs)}")
    total_start = time.time()

    results = []
    for run_index, params in enumerate(configs, start=1):
        results.append(run_single_backtest(run_index, params, module))

    batch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = build_summary_frame(results, batch_time)
    write_summary(summary)

    completed = [r for r in results if r["status"] == "completed"]
    completed.sort(key=lambda item: item.get("score", float("-inf")), reverse=True)
    print("\nTop runs by score:")
    for run in completed[:10]:
        metrics = run["metrics"]
        print(
            f"  {run.get('score', 0):8.3f} | {run['run_name']} | "
            f"ann={metrics['Strategy Ann. Return']} | "
            f"EW={metrics['Benchmark (EW) Ann. Return']} | "
            f"IR(EW)={metrics['Information Ratio (vs EW)']} | "
            f"Sharpe={metrics['Strategy Sharpe']}"
        )
    print(f"\nTotal elapsed={(time.time() - total_start) / 60.0:.1f}m")


if __name__ == "__main__":
    run_parameter_search()
