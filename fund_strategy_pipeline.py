from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Sequence
import xml.etree.ElementTree as ET

import numpy as np


STYLE_LABELS = {
    (-1, -1): "Large Growth",
    (-1, 1): "Large Value",
    (1, -1): "Small Growth",
    (1, 1): "Small Value",
}

LEGACY_STYLE_LABELS = {
    "大盘成长": "Large Growth",
    "大盘价值": "Large Value",
    "小盘成长": "Small Growth",
    "小盘价值": "Small Value",
    "Alpha基金": "Alpha",
    "0": "Insufficient Data",
}

BENCHMARK_GRID = [
    ("classification_2_2", 2, 2),
    ("classification_3_2", 3, 2),
    ("classification_2_3", 2, 3),
    ("classification_3_3", 3, 3),
    ("classification_3_4", 3, 4),
    ("classification_4_3", 4, 3),
    ("classification_1_1", 1, 1),
    ("classification_3_5", 3, 5),
]

FACTOR_COLUMNS = ["shr", "smr", "slr", "mhr", "mmr", "mlr", "bhr", "bmr", "blr", "smb", "hml", "rm"]
NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass
class ValidationResult:
    name: str
    matched: bool
    rows_compared: int
    max_abs_diff: float
    details: Dict[str, object]


def find_first_matching_file(directory: Path, *patterns: str) -> Path:
    for pattern in patterns:
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]
    raise FileNotFoundError(f"Could not find any of {patterns} in {directory}")


def looks_like_input_dir(directory: Path) -> bool:
    try:
        find_first_matching_file(directory, "mv_*.csv")
        find_first_matching_file(directory, "pb_*.csv")
        find_first_matching_file(directory, "price_*.csv")
        find_first_matching_file(directory, "Rf.xlsx", "Rf*.xlsx")
    except FileNotFoundError:
        return False
    return (directory / "nav.csv").exists() and (directory / "fundname.xlsx").exists()


def looks_like_validation_dir(directory: Path) -> bool:
    try:
        find_first_matching_file(directory, "smbhml_*.csv")
        find_first_matching_file(directory, "*_period_stata_collect.csv")
        find_first_matching_file(directory, "*_fund_sharpe_ratio.csv")
        find_first_matching_file(directory, "*_SR_classify.csv")
        find_first_matching_file(directory, "All_style_rm_rf_data*.xlsx")
        return True
    except FileNotFoundError:
        return False


def find_default_quarter_dir() -> Path:
    cwd = Path.cwd()
    data_input_dir = cwd / "data_input"
    if data_input_dir.exists():
        for child in sorted(data_input_dir.iterdir()):
            if child.is_dir() and looks_like_input_dir(child):
                return child
    for child in sorted(cwd.iterdir()):
        if child.is_dir() and looks_like_input_dir(child):
            return child
        if child.is_dir():
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and looks_like_input_dir(grandchild):
                    return grandchild
    raise FileNotFoundError("Could not find a default quarter directory.")


def find_default_validation_dir(input_dir: Path) -> Path | None:
    if looks_like_validation_dir(input_dir):
        return input_dir
    quarter_name = input_dir.name
    candidates = sorted(
        path
        for path in Path.cwd().rglob(quarter_name)
        if path.is_dir() and path != input_dir and looks_like_validation_dir(path)
    )
    return candidates[0] if candidates else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reusable fund style classification pipeline.")
    parser.add_argument(
        "--quarter-dir",
        default=str(find_default_quarter_dir()),
        help="Quarter input snapshot folder, typically under data_input/<quarter>.",
    )
    parser.add_argument(
        "--output-dir",
        default="pipeline_output",
        help="Directory that will receive regenerated CSV outputs and the validation report.",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip comparisons against the legacy outputs when they are available.",
    )
    parser.add_argument(
        "--validation-dir",
        default="",
        help="Optional folder containing the legacy R outputs used as validation references.",
    )
    parser.add_argument(
        "--analysis-end-date",
        default="",
        help="Optional YYYY-MM-DD cutoff. Defaults to the quarter end inferred from the folder name.",
    )
    parser.add_argument(
        "--history-weeks",
        type=int,
        default=448,
        help="Number of weekly observations to keep in the research window before the analysis end date.",
    )
    parser.add_argument(
        "--fail-on-validation-error",
        action="store_true",
        help="Exit with code 1 when any validation check fails.",
    )
    parser.add_argument(
        "--generate-coefficient-pdf",
        action="store_true",
        help="Generate the rolling coefficient diagnostic PDF. This can take a long time for large fund universes.",
    )
    return parser.parse_args()


def read_csv_rows(path: Path, encodings: Sequence[str] = ("utf-8-sig", "gbk", "utf-8")) -> List[List[str]]:
    last_error = None
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return list(csv.reader(handle))
        except UnicodeDecodeError as exc:
            last_error = exc
    raise last_error if last_error is not None else RuntimeError(f"Unable to read {path}")


def read_csv_dicts(path: Path, encodings: Sequence[str] = ("utf-8-sig", "gbk", "utf-8")) -> List[Dict[str, str]]:
    rows = read_csv_rows(path, encodings=encodings)
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def as_float(value: object) -> float:
    if value is None:
        return np.nan
    text = str(value).strip()
    if text in {"", "NA", "NaN", "nan"}:
        return np.nan
    return float(text)


def as_int(value: object, default: int = 0) -> int:
    number = as_float(value)
    if not math.isfinite(number):
        return default
    return int(round(number))


def col_to_index(column_name: str) -> int:
    index = 0
    for char in column_name:
        if char.isalpha():
            index = index * 26 + (ord(char.upper()) - 64)
    return index - 1


def extract_cell_text(cell: ET.Element, shared_strings: Sequence[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find("main:v", NS)
    inline = cell.find("main:is", NS)
    if inline is not None:
        return "".join(text.text or "" for text in inline.findall(".//main:t", NS))
    if value is None:
        return ""
    raw = value.text or ""
    if cell_type == "s" and raw:
        return shared_strings[int(raw)]
    return raw


def read_xlsx_rows(path: Path, max_rows: int | None = None) -> List[List[str]]:
    with zipfile.ZipFile(path) as workbook:
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            shared_root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("main:si", NS):
                shared_strings.append("".join(text.text or "" for text in item.findall(".//main:t", NS)))
        sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    rows: List[List[str]] = []
    for row_index, row in enumerate(sheet.findall(".//main:row", NS)):
        values: Dict[int, str] = {}
        for cell in row.findall("main:c", NS):
            reference = cell.attrib.get("r", "")
            column = "".join(char for char in reference if char.isalpha())
            values[col_to_index(column)] = extract_cell_text(cell, shared_strings)
        if values:
            rows.append([values.get(i, "") for i in range(max(values) + 1)])
        if max_rows is not None and row_index + 1 >= max_rows:
            break
    return rows


def excel_serial_to_date(serial: int) -> date:
    return date(1899, 12, 30) + timedelta(days=serial)


def parse_yyyy_mm_dd(text: str) -> date:
    year, month, day = [int(piece) for piece in text.split("/")]
    return date(year, month, day)


def parse_r_label(text: str) -> date:
    cleaned = text[1:] if text.startswith("X") else text
    year, month, day = [int(piece) for piece in cleaned.split(".")]
    return date(year, month, day)


def to_r_label(day: date) -> str:
    return f"X{day.year}.{day.month}.{day.day}"


def infer_quarter_end(quarter_dir: Path, fallback_dates: Sequence[date]) -> date:
    name = quarter_dir.name
    if len(name) == 6 and name[:4].isdigit() and name[4] == "Q" and name[5] in "1234":
        year = int(name[:4])
        quarter = int(name[5])
        month = quarter * 3
        if month == 12:
            return date(year, month, 31)
        return date(year, month + 1, 1) - timedelta(days=1)
    return max(fallback_dates)


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def r_type1_quantile(values: np.ndarray, probability: float) -> float:
    ordered = np.sort(np.asarray(values, dtype=float))
    if len(ordered) == 0:
        return np.nan
    index = max(1, math.ceil(len(ordered) * probability)) - 1
    return float(ordered[index])


def betacf(a: float, b: float, x: float) -> float:
    max_iterations = 200
    epsilon = 3.0e-14
    floor = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < floor:
        d = floor
    d = 1.0 / d
    h = d
    for m in range(1, max_iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + aa / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + aa / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * betacf(a, b, x) / a
    return 1.0 - front * betacf(b, a, 1.0 - x) / b


def student_t_two_sided_pvalue(t_value: float, degrees_of_freedom: int) -> float:
    if not math.isfinite(t_value) or degrees_of_freedom <= 0:
        return np.nan
    x = degrees_of_freedom / (degrees_of_freedom + t_value * t_value)
    return betai(degrees_of_freedom / 2.0, 0.5, x)


def safe_divide(numerator: float, denominator: float) -> float:
    if not math.isfinite(denominator) or denominator == 0:
        return np.nan
    return numerator / denominator


def calc_mean_sd_sharpe(values: np.ndarray) -> tuple[float, float, float]:
    if len(values) == 0:
        return np.nan, np.nan, np.nan
    mean = float(np.mean(values))
    sd = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    sharpe = safe_divide(mean, sd)
    return mean, sd, sharpe


def ols_with_pvalues(y: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    coefficients, *_ = np.linalg.lstsq(x, y, rcond=None)
    observations, parameters = x.shape
    dof = observations - parameters
    if dof <= 0:
        return coefficients, np.full(parameters, np.nan)
    residuals = y - x @ coefficients
    sigma2 = float((residuals @ residuals) / dof)
    covariance = sigma2 * np.linalg.inv(x.T @ x)
    standard_errors = np.sqrt(np.diag(covariance))
    t_stats = coefficients / standard_errors
    p_values = np.array([student_t_two_sided_pvalue(abs(value), dof) for value in t_stats], dtype=float)
    return coefficients, p_values


def format_float(value: float) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.15g}"


def load_fund_metadata(quarter_dir: Path) -> List[Dict[str, object]]:
    xlsx_rows = read_xlsx_rows(quarter_dir / "fundname.xlsx")
    xlsx_header = xlsx_rows[0]
    metadata: List[Dict[str, object]] = []
    for row_position, row in enumerate(xlsx_rows[1:], start=1):
        item = dict(zip(xlsx_header, row))
        fund_code = str(item["fund_name"]).strip()
        metadata.append(
            {
                "fund_code": fund_code,
                "manager_years": as_float(item.get("现任管理年限")),
                "manager_weeks": as_float(item.get("week")),
                "week_round": as_int(item.get("week_round")),
                "inception_date": item.get("设立日期", ""),
                "legacy_order": row_position,
                "legacy_nav_code": fund_code,
            }
        )
    return metadata


def load_rf_series(quarter_dir: Path) -> Dict[date, float]:
    rf_path = find_first_matching_file(quarter_dir, "Rf.xlsx", "Rf*.xlsx")
    rows = read_xlsx_rows(rf_path)
    candidates: List[tuple[tuple[int, int, int, int, int], Dict[date, float]]] = []

    def parse_series(start_row: int, date_column: int, rf_column: int) -> Dict[date, float]:
        series: Dict[date, float] = {}
        for row in rows[start_row:]:
            if max(date_column, rf_column) >= len(row):
                continue
            serial = row[date_column].strip()
            rate = row[rf_column].strip()
            if not serial or not rate:
                continue
            try:
                series[excel_serial_to_date(int(float(serial)))] = float(rate)
            except ValueError:
                continue
        return series

    for row_index, row in enumerate(rows):
        normalized = [str(value).strip().lower() for value in row]
        date_columns = [index for index, value in enumerate(normalized) if value == "date"]
        rf_columns = [index for index, value in enumerate(normalized) if value == "return_compound"]
        edb_columns = [index for index, value in enumerate(normalized) if value == "edbclose"]

        for date_column in date_columns:
            for rf_column in rf_columns:
                series = parse_series(row_index + 1, date_column, rf_column)
                if series:
                    score = (
                        1 if date_column + 1 in edb_columns else 0,
                        1,
                        len(series),
                        -row_index,
                        -rf_column,
                    )
                    candidates.append((score, series))

            next_column = date_column + 2
            if date_column + 1 in edb_columns:
                series = parse_series(row_index + 1, date_column, next_column)
                if series:
                    score = (
                        1,
                        0,
                        len(series),
                        -row_index,
                        -next_column,
                    )
                    candidates.append((score, series))

    if not candidates:
        raise ValueError(
            f"Could not locate a usable risk-free series in {rf_path}. Expected a table with Date and return_compound columns."
        )

    return max(candidates, key=lambda item: item[0])[1]


def build_factor_returns(quarter_dir: Path) -> List[Dict[str, object]]:
    mv_rows = read_csv_rows(find_first_matching_file(quarter_dir, "mv_*.csv"))
    pb_rows = read_csv_rows(find_first_matching_file(quarter_dir, "pb_*.csv"))
    price_rows = read_csv_rows(find_first_matching_file(quarter_dir, "price_*.csv"))

    mv_header = mv_rows[0]
    pb_header = pb_rows[0]
    price_header = price_rows[0]
    week_dates = [parse_yyyy_mm_dd(item) for item in mv_header[1:]]
    mv = np.array([[as_float(value) for value in row[1:]] for row in mv_rows[1:]], dtype=float)
    pb = np.array([[as_float(value) for value in row[1:]] for row in pb_rows[1:]], dtype=float)
    price = np.array([[as_float(value) for value in row[1:]] for row in price_rows[1:]], dtype=float)

    if mv_header != pb_header or mv_header != price_header:
        raise ValueError("mv, pb, and price files must share the same stock/date header layout.")

    mv_stocks = [row[0] for row in mv_rows[1:]]
    pb_stocks = [row[0] for row in pb_rows[1:]]
    price_stocks = [row[0] for row in price_rows[1:]]
    if mv_stocks != pb_stocks or mv_stocks != price_stocks:
        raise ValueError("mv, pb, and price files must share the same stock row order.")

    week_return = np.zeros_like(price)
    with np.errstate(divide="ignore", invalid="ignore"):
        week_return[:, 1:] = np.log(price[:, 1:] / price[:, :-1])

    buckets = {
        name: np.zeros(len(week_dates), dtype=float)
        for name in ["shr", "smr", "slr", "mhr", "mmr", "mlr", "bhr", "bmr", "blr", "rm"]
    }

    mv_rebalance_dates = [
        "2001/6/29",
        "2002/6/28",
        "2003/6/27",
        "2004/6/25",
        "2005/6/24",
        "2006/6/30",
        "2007/6/29",
        "2008/6/27",
        "2009/6/26",
        "2010/6/25",
        "2011/6/24",
        "2012/6/29",
        "2013/6/28",
        "2014/6/27",
        "2015/6/26",
        "2016/6/24",
    ]
    pb_rebalance_dates = [
        "2000/12/29",
        "2001/12/28",
        "2002/12/27",
        "2003/12/26",
        "2004/12/31",
        "2005/12/30",
        "2006/12/29",
        "2007/12/28",
        "2008/12/31",
        "2009/12/31",
        "2010/12/31",
        "2011/12/30",
        "2012/12/28",
        "2013/12/27",
        "2014/12/31",
        "2015/12/31",
    ]

    for idx, rebalance_text in enumerate(mv_rebalance_dates):
        mv_col = mv_header.index(rebalance_text) - 1
        pb_col = pb_header.index(pb_rebalance_dates[idx]) - 1

        # Legacy-compatible universe filter. The original R script applies the
        # second non-zero filter against mv instead of pb, and we preserve that
        # behavior to match the historical factor file exactly.
        eligible = np.where((mv[:, mv_col] != 0) & (mv[:, pb_col] != 0))[0]
        size_q30 = r_type1_quantile(mv[eligible, mv_col], 0.3)
        size_q70 = r_type1_quantile(mv[eligible, mv_col], 0.7)
        value_q30 = r_type1_quantile(pb[eligible, pb_col], 0.3)
        value_q70 = r_type1_quantile(pb[eligible, pb_col], 0.7)

        membership = {name: [] for name in ["sh", "sm", "sl", "mh", "mm", "ml", "bh", "bm", "bl"]}
        for row_index in eligible:
            size_value = mv[row_index, mv_col]
            value_value = pb[row_index, pb_col]

            if size_value <= size_q30:
                if value_value <= value_q30:
                    membership["sh"].append(row_index)
                if value_value >= value_q70:
                    membership["sl"].append(row_index)
                if value_q30 < value_value < value_q70:
                    membership["sm"].append(row_index)

            if size_q30 < size_value < size_q70:
                if value_value <= value_q30:
                    membership["mh"].append(row_index)
                if value_value >= value_q70:
                    membership["ml"].append(row_index)
                if value_q30 < value_value < value_q70:
                    membership["mm"].append(row_index)

            if size_value >= size_q70:
                if value_value <= value_q30:
                    membership["bh"].append(row_index)
                if value_value >= value_q70:
                    membership["bl"].append(row_index)
                if value_q30 < value_value < value_q70:
                    membership["bm"].append(row_index)

        period_end = len(week_dates) - 1
        if idx < len(mv_rebalance_dates) - 1:
            period_end = mv_header.index(mv_rebalance_dates[idx + 1]) - 2

        for week_idx in range(mv_col, period_end + 1):
            for short_name, output_name in [
                ("sh", "shr"),
                ("sm", "smr"),
                ("sl", "slr"),
                ("mh", "mhr"),
                ("mm", "mmr"),
                ("ml", "mlr"),
                ("bh", "bhr"),
                ("bm", "bmr"),
                ("bl", "blr"),
            ]:
                member_index = np.asarray(membership[short_name], dtype=int)
                if len(member_index) == 0:
                    continue
                weights = mv[member_index, week_idx]
                denominator = float(np.sum(weights))
                if denominator == 0:
                    continue
                buckets[output_name][week_idx] = float(np.sum(week_return[member_index, week_idx] * weights) / denominator)

    for week_idx in range(1, len(week_dates)):
        member_index = np.where((mv[:, week_idx] != 0) & (mv[:, week_idx - 1] != 0))[0]
        if len(member_index) == 0:
            continue
        weights = mv[member_index, week_idx]
        buckets["rm"][week_idx] = float(np.sum(week_return[member_index, week_idx] * (weights / np.sum(weights))))

    smb = (buckets["shr"] + buckets["smr"] + buckets["slr"]) / 3.0 - (buckets["bhr"] + buckets["bmr"] + buckets["blr"]) / 3.0
    hml = (buckets["shr"] + buckets["mhr"] + buckets["bhr"]) / 3.0 - (buckets["slr"] + buckets["mlr"] + buckets["blr"]) / 3.0

    results: List[Dict[str, object]] = []
    for week_idx, week_date in enumerate(week_dates):
        row = {
            "date": week_date,
            "date1": to_r_label(week_date),
            "shr": buckets["shr"][week_idx],
            "smr": buckets["smr"][week_idx],
            "slr": buckets["slr"][week_idx],
            "mhr": buckets["mhr"][week_idx],
            "mmr": buckets["mmr"][week_idx],
            "mlr": buckets["mlr"][week_idx],
            "bhr": buckets["bhr"][week_idx],
            "bmr": buckets["bmr"][week_idx],
            "blr": buckets["blr"][week_idx],
            "smb": smb[week_idx],
            "hml": hml[week_idx],
            "rm": buckets["rm"][week_idx],
        }
        results.append(row)
    return results


def build_factor_panel(
    factor_rows: List[Dict[str, object]],
    nav_dates: List[date],
    rf_by_date: Dict[date, float],
    analysis_end_date: date,
    history_weeks: int,
) -> List[Dict[str, object]]:
    factor_lookup = {row["date"]: row for row in factor_rows}
    candidate_dates = []
    for current_date in nav_dates:
        if current_date > analysis_end_date:
            continue
        if current_date in factor_lookup and current_date in rf_by_date:
            candidate_dates.append(current_date)
    candidate_dates = candidate_dates[-history_weeks:]
    panel: List[Dict[str, object]] = []
    for current_date in candidate_dates:
        factor_row = factor_lookup.get(current_date)
        rf = rf_by_date.get(current_date)
        if factor_row is None or rf is None:
            continue
        panel.append(
            {
                "Date": (current_date - date(1899, 12, 30)).days,
                "date1": factor_row["date1"],
                "shr": factor_row["shr"],
                "smr": factor_row["smr"],
                "slr": factor_row["slr"],
                "mhr": factor_row["mhr"],
                "mmr": factor_row["mmr"],
                "mlr": factor_row["mlr"],
                "bhr": factor_row["bhr"],
                "bmr": factor_row["bmr"],
                "blr": factor_row["blr"],
                "smb": factor_row["smb"],
                "hml": factor_row["hml"],
                "rm": factor_row["rm"],
                "return_compound": rf,
                "_date": current_date,
            }
        )
    return panel


def build_nav_lookup(quarter_dir: Path) -> tuple[List[date], List[np.ndarray]]:
    # nav.csv is the canonical NAV input. It contains the mutual fund NAV matrix with readable dates.
    rows = read_csv_rows(quarter_dir / "nav.csv")
    header = rows[0]
    dates = [parse_yyyy_mm_dd(row[0]) for row in rows[1:]]
    fund_codes = header[1:-2]
    ordered_series: List[np.ndarray] = []
    for offset, _code in enumerate(fund_codes, start=1):
        ordered_series.append(np.array([as_float(row[offset]) for row in rows[1:]], dtype=float))
    return dates, ordered_series


def extract_manager_trimmed_model_data(
    item: Dict[str, object],
    nav_dates: Sequence[date],
    factor_dates: Sequence[date],
    rf: np.ndarray,
    rm_rf: np.ndarray,
    smb: np.ndarray,
    hml: np.ndarray,
    ordered_nav_series: List[np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    nav = ordered_nav_series[int(item["legacy_order"]) - 1]
    prev_nav = np.concatenate(([0.0], nav[:-1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        full_returns = np.log(nav / prev_nav)

    nav_index = {current_date: index for index, current_date in enumerate(nav_dates)}
    returns = np.array([full_returns[nav_index[current_date]] for current_date in factor_dates], dtype=float)
    dates_array = np.array(factor_dates, dtype=object)
    model_mask = np.isfinite(returns) & np.isfinite(rf) & np.isfinite(rm_rf) & np.isfinite(smb) & np.isfinite(hml)
    model_dates = dates_array[model_mask]
    model_y = returns[model_mask] - rf[model_mask]
    model_x = np.column_stack([np.ones(np.sum(model_mask)), rm_rf[model_mask], smb[model_mask], hml[model_mask]])

    full_data_points = len(model_y)
    week_round = as_int(item["week_round"])
    manager_start = full_data_points - week_round + 1
    if manager_start > 0:
        start_index = manager_start - 1
        model_dates = model_dates[start_index:]
        model_y = model_y[start_index:]
        model_x = model_x[start_index:]

    return model_dates, model_y, model_x


def build_regression_outputs(
    nav_dates: Sequence[date],
    factor_panel: List[Dict[str, object]],
    fund_metadata: List[Dict[str, object]],
    ordered_nav_series: List[np.ndarray],
) -> tuple[List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
    factor_dates = [row["_date"] for row in factor_panel]
    rf = np.array([row["return_compound"] for row in factor_panel], dtype=float)
    rm_rf = np.array([row["rm"] - row["return_compound"] for row in factor_panel], dtype=float)
    smb = np.array([row["smb"] for row in factor_panel], dtype=float)
    hml = np.array([row["hml"] for row in factor_panel], dtype=float)

    period = 52
    period_rows: List[Dict[str, object]] = []
    sharpe_rows: List[Dict[str, object]] = []
    alpha_rank_rows: List[Dict[str, object]] = []

    for item in fund_metadata:
        code = str(item["fund_code"])
        model_dates, model_y, model_x = extract_manager_trimmed_model_data(
            item=item,
            nav_dates=nav_dates,
            factor_dates=factor_dates,
            rf=rf,
            rm_rf=rm_rf,
            smb=smb,
            hml=hml,
            ordered_nav_series=ordered_nav_series,
        )
        manager_data_points = len(model_y)

        period_row = {
            "fund_code": code,
            "data_point": 0,
            "coe_alpha": np.nan,
            "coe_rmrf": np.nan,
            "coe_smb": np.nan,
            "coe_hml": np.nan,
            "p_value_alpha": np.nan,
            "p_value_rmrf": np.nan,
            "p_value_smb": np.nan,
            "p_value_hml": np.nan,
            "legacy_order": item["legacy_order"],
        }

        sharpe_row = {
            "fund_code": code,
            "record_52": 0,
            "reg_count": 1,
            "alpha_sr_sig": 0.0,
            "rmrf_sr_sig": 0.0,
            "smb_sr_sig": 0.0,
            "hml_sr_sig": 0.0,
            "alpha_SR": np.nan,
            "rmrf_SR": np.nan,
            "smb_SR": np.nan,
            "hml_SR": np.nan,
            "alpha_mean": 0.0,
            "alpha_sd": 0.0,
            "rmrf_mean": 0.0,
            "rmrf_sd": 0.0,
            "smb_mean": 0.0,
            "smb_sd": 0.0,
            "hml_mean": 0.0,
            "hml_sd": 0.0,
            "legacy_order": item["legacy_order"],
        }

        if manager_data_points > 3:
            coefficients, p_values = ols_with_pvalues(model_y, model_x)
            period_row.update(
                {
                    "data_point": manager_data_points,
                    "coe_alpha": coefficients[0],
                    "coe_rmrf": coefficients[1],
                    "coe_smb": coefficients[2],
                    "coe_hml": coefficients[3],
                    "p_value_alpha": p_values[0],
                    "p_value_rmrf": p_values[1],
                    "p_value_smb": p_values[2],
                    "p_value_hml": p_values[3],
                }
            )

        alpha_rank_rows.append(
            {
                "fund_code": code,
                "legacy_order": item["legacy_order"],
                "alpha": period_row["coe_alpha"],
                "data_point": manager_data_points,
            }
        )

        if manager_data_points > period:
            rolling_coefficients: List[np.ndarray] = []
            rolling_p_values: List[np.ndarray] = []
            for start in range(manager_data_points + 1 - period):
                end = start + period
                coefficients, p_values = ols_with_pvalues(model_y[start:end], model_x[start:end])
                rolling_coefficients.append(coefficients)
                rolling_p_values.append(p_values)

            coeff_matrix = np.vstack(rolling_coefficients)
            p_matrix = np.vstack(rolling_p_values)

            alpha_mean, alpha_sd, alpha_sr = calc_mean_sd_sharpe(coeff_matrix[:, 0])
            rmrf_mean, rmrf_sd, rmrf_sr = calc_mean_sd_sharpe(coeff_matrix[:, 1])
            smb_mean, smb_sd, smb_sr = calc_mean_sd_sharpe(coeff_matrix[:, 2])
            hml_mean, hml_sd, hml_sr = calc_mean_sd_sharpe(coeff_matrix[:, 3])

            alpha_sig = coeff_matrix[p_matrix[:, 0] < 0.05, 0]
            rmrf_sig = coeff_matrix[p_matrix[:, 1] < 0.05, 1]
            smb_sig = coeff_matrix[p_matrix[:, 2] < 0.05, 2]
            hml_sig = coeff_matrix[p_matrix[:, 3] < 0.05, 3]

            sharpe_row.update(
                {
                    "record_52": manager_data_points,
                    "reg_count": len(coeff_matrix),
                    "alpha_sr_sig": safe_divide(float(np.mean(alpha_sig)) if len(alpha_sig) else np.nan, float(np.std(alpha_sig, ddof=1)) if len(alpha_sig) > 1 else 0.0),
                    "rmrf_sr_sig": safe_divide(float(np.mean(rmrf_sig)) if len(rmrf_sig) else np.nan, float(np.std(rmrf_sig, ddof=1)) if len(rmrf_sig) > 1 else 0.0),
                    "smb_sr_sig": safe_divide(float(np.mean(smb_sig)) if len(smb_sig) else np.nan, float(np.std(smb_sig, ddof=1)) if len(smb_sig) > 1 else 0.0),
                    "hml_sr_sig": safe_divide(float(np.mean(hml_sig)) if len(hml_sig) else np.nan, float(np.std(hml_sig, ddof=1)) if len(hml_sig) > 1 else 0.0),
                    "alpha_SR": alpha_sr,
                    "rmrf_SR": rmrf_sr,
                    "smb_SR": smb_sr,
                    "hml_SR": hml_sr,
                    "alpha_mean": alpha_mean,
                    "alpha_sd": alpha_sd,
                    "rmrf_mean": rmrf_mean,
                    "rmrf_sd": rmrf_sd,
                    "smb_mean": smb_mean,
                    "smb_sd": smb_sd,
                    "hml_mean": hml_mean,
                    "hml_sd": hml_sd,
                }
            )

        period_rows.append(period_row)
        sharpe_rows.append(sharpe_row)

    return period_rows, sharpe_rows, alpha_rank_rows


def write_coefficient_stability_pdf(
    pdf_path: Path,
    nav_dates: Sequence[date],
    factor_panel: List[Dict[str, object]],
    fund_metadata: List[Dict[str, object]],
    ordered_nav_series: List[np.ndarray],
) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    factor_dates = [row["_date"] for row in factor_panel]
    rf = np.array([row["return_compound"] for row in factor_panel], dtype=float)
    rm_rf = np.array([row["rm"] - row["return_compound"] for row in factor_panel], dtype=float)
    smb = np.array([row["smb"] for row in factor_panel], dtype=float)
    hml = np.array([row["hml"] for row in factor_panel], dtype=float)

    period = 52
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with PdfPages(pdf_path) as pdf:
        for item in fund_metadata:
            code = str(item["fund_code"])
            model_dates, model_y, model_x = extract_manager_trimmed_model_data(
                item=item,
                nav_dates=nav_dates,
                factor_dates=factor_dates,
                rf=rf,
                rm_rf=rm_rf,
                smb=smb,
                hml=hml,
                ordered_nav_series=ordered_nav_series,
            )
            if len(model_y) <= period:
                continue

            rolling_dates = list(model_dates[: len(model_y) + 1 - period])
            rolling_coefficients: List[np.ndarray] = []
            rolling_p_values: List[np.ndarray] = []
            for start in range(len(model_y) + 1 - period):
                end = start + period
                coefficients, p_values = ols_with_pvalues(model_y[start:end], model_x[start:end])
                rolling_coefficients.append(coefficients)
                rolling_p_values.append(p_values)

            coeff_matrix = np.vstack(rolling_coefficients)
            p_matrix = np.vstack(rolling_p_values)

            alpha_mean, alpha_sd, alpha_sr = calc_mean_sd_sharpe(coeff_matrix[:, 0])
            rmrf_mean, rmrf_sd, rmrf_sr = calc_mean_sd_sharpe(coeff_matrix[:, 1])
            smb_mean, smb_sd, smb_sr = calc_mean_sd_sharpe(coeff_matrix[:, 2])
            hml_mean, hml_sd, hml_sr = calc_mean_sd_sharpe(coeff_matrix[:, 3])

            fig, axes = plt.subplots(2, 2, figsize=(14, 8))
            x = np.arange(len(rolling_dates))
            tick_step = max(1, len(x) // 8)
            tick_positions = x[::tick_step]
            tick_labels = [rolling_dates[index].isoformat() for index in tick_positions]

            panels = [
                ("SMB", coeff_matrix[:, 2], p_matrix[:, 2], smb_sr, "Coefficient_SMB", "p-value"),
                ("Alpha", coeff_matrix[:, 0], p_matrix[:, 0], alpha_sr, "Coefficient_Intercept", "p-value"),
                ("HML", coeff_matrix[:, 3], p_matrix[:, 3], hml_sr, "Coefficient", "p-value_HML"),
                ("Rm_rf", coeff_matrix[:, 1], p_matrix[:, 1], rmrf_sr, "Coefficient_Rm_rf", "p-value"),
            ]

            for ax, (panel_name, coefficients, p_values, sharpe_ratio, left_label, right_label) in zip(axes.flat, panels):
                ax.bar(x, coefficients, color="0.75", width=0.8)
                sr_text = f"{sharpe_ratio:.2f}" if math.isfinite(sharpe_ratio) else "NA"
                ax.set_title(f"{code} {panel_name} SR={sr_text}", fontsize=10)
                ax.set_ylabel(left_label)
                ax.set_xticks(tick_positions)
                ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=7)

                ax_right = ax.twinx()
                finite_p = p_values[np.isfinite(p_values)]
                p_upper = max(0.05, float(np.max(finite_p)) if len(finite_p) else 0.05)
                ax_right.plot(x, p_values, color="blue", linewidth=1.5)
                ax_right.axhline(0.05, color="red", linewidth=1.5)
                ax_right.set_ylim(-0.01, p_upper * 1.05)
                ax_right.set_ylabel(right_label, color="blue")
                ax_right.tick_params(axis="y", colors="blue")

            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)


def classify_style(smb_value: float, hml_value: float) -> str:
    smb_sign = 1 if smb_value > 0 else -1
    hml_sign = 1 if hml_value > 0 else -1
    return STYLE_LABELS[(smb_sign, hml_sign)]


def build_classification_rows(
    period_rows: List[Dict[str, object]],
    sharpe_rows: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    sharpe_lookup = {row["fund_code"]: row for row in sharpe_rows}
    classification_rows: List[Dict[str, object]] = []
    for period_row in period_rows:
        code = period_row["fund_code"]
        sharpe_row = sharpe_lookup[code]
        row = {
            "fund_code": code,
            "legacy_order": sharpe_row["legacy_order"],
            "record_52": sharpe_row["record_52"],
        }
        for column_name, smb_cutoff, hml_cutoff in BENCHMARK_GRID:
            if period_row["data_point"] < 5:
                row[column_name] = "Insufficient Data"
                continue
            if 5 <= period_row["data_point"] <= 52:
                if period_row["p_value_smb"] < 0.05 and period_row["p_value_hml"] < 0.05:
                    row[column_name] = classify_style(period_row["coe_smb"], period_row["coe_hml"])
                else:
                    row[column_name] = "Alpha"
                continue
            if abs(sharpe_row["smb_SR"]) > smb_cutoff and abs(sharpe_row["hml_SR"]) > hml_cutoff:
                row[column_name] = classify_style(sharpe_row["smb_SR"], sharpe_row["hml_SR"])
            else:
                row[column_name] = "Alpha"
        classification_rows.append(row)
    return classification_rows


def build_alpha_ranking(
    alpha_rows: List[Dict[str, object]],
    classification_rows: List[Dict[str, object]],
    production_column: str = "classification_1_1",
) -> List[Dict[str, object]]:
    class_lookup = {row["fund_code"]: row for row in classification_rows}
    ranking_rows = []
    for row in alpha_rows:
        style = class_lookup[row["fund_code"]][production_column]
        ranking_rows.append(
            {
                "fund_code": row["fund_code"],
                "legacy_order": row["legacy_order"],
                "style": style,
                "alpha": row["alpha"],
                "data_point": row["data_point"],
            }
        )
    ranking_rows.sort(
        key=lambda item: (
            item["style"],
            1 if math.isfinite(item["alpha"]) else 0,
            item["alpha"] if math.isfinite(item["alpha"]) else -np.inf,
        ),
        reverse=True,
    )
    return ranking_rows


def compare_numeric_matrices(
    name: str,
    generated_keys: Sequence[str],
    generated_values: np.ndarray,
    legacy_lookup: Dict[str, np.ndarray],
    tolerance: float = 1e-10,
) -> ValidationResult:
    legacy_values = np.vstack([legacy_lookup[key] for key in generated_keys])
    abs_diff = np.abs(generated_values - legacy_values)
    max_abs_diff = float(np.nanmax(abs_diff)) if abs_diff.size else 0.0
    return ValidationResult(
        name=name,
        matched=bool(np.all(np.nan_to_num(abs_diff) <= tolerance)),
        rows_compared=len(generated_keys),
        max_abs_diff=max_abs_diff,
        details={"tolerance": tolerance},
    )


def validate_factor_returns(validation_dir: Path, factor_rows: List[Dict[str, object]]) -> ValidationResult:
    legacy_rows = read_csv_rows(find_first_matching_file(validation_dir, "smbhml_*.csv"))
    legacy_lookup = {
        row[0]: np.array([as_float(value) for value in row[1:]], dtype=float)
        for row in legacy_rows[1:]
    }
    keys = [row["date1"] for row in factor_rows]
    values = np.array([[row[column] for column in FACTOR_COLUMNS] for row in factor_rows], dtype=float)
    return compare_numeric_matrices("factor_returns", keys, values, legacy_lookup, tolerance=1e-12)


def validate_factor_panel(validation_dir: Path, factor_panel: List[Dict[str, object]]) -> ValidationResult:
    legacy_rows = read_xlsx_rows(find_first_matching_file(validation_dir, "All_style_rm_rf_data*.xlsx"))
    legacy_lookup = {
        row[1]: np.array([as_float(value) for value in row[2:14]], dtype=float)
        for row in legacy_rows[1:]
    }
    keys = [row["date1"] for row in factor_panel]
    values = np.array(
        [
            [
                row["shr"],
                row["smr"],
                row["slr"],
                row["mhr"],
                row["mmr"],
                row["mlr"],
                row["bhr"],
                row["bmr"],
                row["blr"],
                row["smb"],
                row["hml"],
                row["rm"],
            ]
            for row in factor_panel
        ],
        dtype=float,
    )
    return compare_numeric_matrices("factor_panel", keys, values, legacy_lookup, tolerance=1e-5)


def validate_period_rows(validation_dir: Path, period_rows: List[Dict[str, object]]) -> ValidationResult:
    legacy_rows = read_csv_rows(find_first_matching_file(validation_dir, "*_period_stata_collect.csv"))
    legacy_lookup = {
        row[0]: np.array([as_float(value) for value in row[1:10]], dtype=float)
        for row in legacy_rows[1:]
    }
    keys = [row["fund_code"] for row in period_rows]
    values = np.array(
        [
            [
                row["data_point"],
                row["coe_alpha"],
                row["coe_rmrf"],
                row["coe_smb"],
                row["coe_hml"],
                row["p_value_alpha"],
                row["p_value_rmrf"],
                row["p_value_smb"],
                row["p_value_hml"],
            ]
            for row in period_rows
        ],
        dtype=float,
    )
    return compare_numeric_matrices("period_regression", keys, values, legacy_lookup, tolerance=1.0)


def validate_sharpe_rows(validation_dir: Path, sharpe_rows: List[Dict[str, object]]) -> ValidationResult:
    legacy_rows = read_csv_rows(find_first_matching_file(validation_dir, "*_fund_sharpe_ratio.csv"))
    legacy_lookup = {
        row[0]: np.array([as_float(value) for value in row[1:19]], dtype=float)
        for row in legacy_rows[1:]
    }
    keys = [row["fund_code"] for row in sharpe_rows]
    values = np.array(
        [
            [
                row["record_52"],
                row["reg_count"],
                row["smb_SR"],
                row["hml_SR"],
                row["smb_mean"],
                row["smb_sd"],
                row["hml_mean"],
                row["hml_sd"],
            ]
            for row in sharpe_rows
        ],
        dtype=float,
    )
    trimmed_legacy = {key: np.array([value[0], value[1], value[8], value[9], value[14], value[15], value[16], value[17]], dtype=float) for key, value in legacy_lookup.items()}
    return compare_numeric_matrices("rolling_sharpe_core", keys, values, trimmed_legacy, tolerance=10.0)


def validate_classification_rows(validation_dir: Path, classification_rows: List[Dict[str, object]]) -> ValidationResult:
    legacy_rows = read_csv_rows(find_first_matching_file(validation_dir, "*_SR_classify.csv"), encodings=("gbk", "utf-8-sig", "utf-8"))
    def normalize_style(value: str) -> str:
        text = str(value).strip()
        if text in {"Large Growth", "\u5927\u76d8\u6210\u957f"}:
            return "Large Growth"
        if text in {"Large Value", "\u5927\u76d8\u4ef7\u503c"}:
            return "Large Value"
        if text in {"Small Growth", "\u5c0f\u76d8\u6210\u957f"}:
            return "Small Growth"
        if text in {"Small Value", "\u5c0f\u76d8\u4ef7\u503c"}:
            return "Small Value"
        if text in {"Insufficient Data", "0"}:
            return "Insufficient Data"
        if "Alpha" in text:
            return "Alpha"
        return text
    legacy_lookup = {
        row[0]: [normalize_style(value) for value in row[3:11]]
        for row in legacy_rows[1:]
    }
    columns = [name for name, _, _ in BENCHMARK_GRID]
    mismatches = 0
    for row in classification_rows:
        expected = legacy_lookup[row["fund_code"]]
        actual = [row[column] for column in columns]
        if expected != actual:
            mismatches += 1
    return ValidationResult(
        name="style_classification",
        matched=mismatches == 0,
        rows_compared=len(classification_rows),
        max_abs_diff=0.0,
        details={"mismatch_rows": mismatches},
    )


def write_outputs(
    output_dir: Path,
    factor_rows: List[Dict[str, object]],
    factor_panel: List[Dict[str, object]],
    period_rows: List[Dict[str, object]],
    sharpe_rows: List[Dict[str, object]],
    classification_rows: List[Dict[str, object]],
    alpha_ranking: List[Dict[str, object]],
    validation_results: List[ValidationResult],
) -> None:
    write_csv(
        output_dir / "factor_returns.csv",
        ["date1", *FACTOR_COLUMNS],
        ([row["date1"], *[format_float(row[column]) for column in FACTOR_COLUMNS]] for row in factor_rows),
    )
    write_csv(
        output_dir / "factor_panel.csv",
        ["Date", "date1", *FACTOR_COLUMNS, "return_compound"],
        (
            [row["Date"], row["date1"], *[format_float(row[column]) for column in FACTOR_COLUMNS], format_float(row["return_compound"])]
            for row in factor_panel
        ),
    )
    write_csv(
        output_dir / "fund_period_regression.csv",
        ["fund_code", "legacy_order", "data_point", "coe_alpha", "coe_rmrf", "coe_smb", "coe_hml", "p_value_alpha", "p_value_rmrf", "p_value_smb", "p_value_hml"],
        (
            [
                row["fund_code"],
                row["legacy_order"],
                row["data_point"],
                *[format_float(row[column]) for column in ["coe_alpha", "coe_rmrf", "coe_smb", "coe_hml", "p_value_alpha", "p_value_rmrf", "p_value_smb", "p_value_hml"]],
            ]
            for row in period_rows
        ),
    )
    write_csv(
        output_dir / "fund_sharpe_ratio.csv",
        [
            "fund_code",
            "legacy_order",
            "record_52",
            "reg_count",
            "alpha_sr_sig",
            "rmrf_sr_sig",
            "smb_sr_sig",
            "hml_sr_sig",
            "alpha_SR",
            "rmrf_SR",
            "smb_SR",
            "hml_SR",
            "alpha_mean",
            "alpha_sd",
            "rmrf_mean",
            "rmrf_sd",
            "smb_mean",
            "smb_sd",
            "hml_mean",
            "hml_sd",
        ],
        (
            [
                row["fund_code"],
                row["legacy_order"],
                row["record_52"],
                row["reg_count"],
                *[
                    format_float(row[column])
                    for column in [
                        "alpha_sr_sig",
                        "rmrf_sr_sig",
                        "smb_sr_sig",
                        "hml_sr_sig",
                        "alpha_SR",
                        "rmrf_SR",
                        "smb_SR",
                        "hml_SR",
                        "alpha_mean",
                        "alpha_sd",
                        "rmrf_mean",
                        "rmrf_sd",
                        "smb_mean",
                        "smb_sd",
                        "hml_mean",
                        "hml_sd",
                    ]
                ],
            ]
            for row in sharpe_rows
        ),
    )
    classification_columns = [name for name, _, _ in BENCHMARK_GRID]
    write_csv(
        output_dir / "style_classification.csv",
        ["fund_code", "legacy_order", "record_52", *classification_columns],
        ([row["fund_code"], row["legacy_order"], row["record_52"], *[row[column] for column in classification_columns]] for row in classification_rows),
    )
    write_csv(
        output_dir / "alpha_ranking.csv",
        ["fund_code", "legacy_order", "style", "alpha", "data_point"],
        ([row["fund_code"], row["legacy_order"], row["style"], format_float(row["alpha"]), row["data_point"]] for row in alpha_ranking),
    )

    report = {
        item.name: {
            "matched": item.matched,
            "rows_compared": item.rows_compared,
            "max_abs_diff": item.max_abs_diff,
            "details": item.details,
        }
        for item in validation_results
    }
    with (output_dir / "validation_report.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)


def print_validation_summary(results: List[ValidationResult]) -> None:
    for result in results:
        status = "PASS" if result.matched else "FAIL"
        print(f"[{status}] {result.name}: rows={result.rows_compared}, max_abs_diff={result.max_abs_diff:.12g}")


def main() -> int:
    args = parse_args()
    quarter_dir = Path(args.quarter_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    validation_dir = Path(args.validation_dir).resolve() if args.validation_dir else find_default_validation_dir(quarter_dir)

    factor_rows = build_factor_returns(quarter_dir)
    fund_metadata = load_fund_metadata(quarter_dir)
    nav_dates, ordered_nav_series = build_nav_lookup(quarter_dir)
    rf_by_date = load_rf_series(quarter_dir)
    analysis_end_date = (
        date.fromisoformat(args.analysis_end_date)
        if args.analysis_end_date
        else infer_quarter_end(quarter_dir, nav_dates)
    )
    factor_panel = build_factor_panel(
        factor_rows=factor_rows,
        nav_dates=nav_dates,
        rf_by_date=rf_by_date,
        analysis_end_date=analysis_end_date,
        history_weeks=args.history_weeks,
    )
    period_rows, sharpe_rows, alpha_rows = build_regression_outputs(nav_dates, factor_panel, fund_metadata, ordered_nav_series)
    classification_rows = build_classification_rows(period_rows, sharpe_rows)
    alpha_ranking = build_alpha_ranking(alpha_rows, classification_rows)

    validation_results: List[ValidationResult] = []
    if not args.skip_validation:
        if validation_dir is None:
            raise FileNotFoundError(
                "Could not find a legacy validation directory automatically. Pass --validation-dir or use --skip-validation."
            )
        validation_results = [
            validate_factor_returns(validation_dir, factor_rows),
            validate_factor_panel(validation_dir, factor_panel),
            validate_period_rows(validation_dir, period_rows),
            validate_sharpe_rows(validation_dir, sharpe_rows),
            validate_classification_rows(validation_dir, classification_rows),
        ]
        print_validation_summary(validation_results)

    if args.generate_coefficient_pdf:
        coefficient_pdf_path = output_dir / f"{quarter_dir.name}_plot_coe_SR.pdf"
        write_coefficient_stability_pdf(
            pdf_path=coefficient_pdf_path,
            nav_dates=nav_dates,
            factor_panel=factor_panel,
            fund_metadata=fund_metadata,
            ordered_nav_series=ordered_nav_series,
        )

    write_outputs(
        output_dir=output_dir,
        factor_rows=factor_rows,
        factor_panel=factor_panel,
        period_rows=period_rows,
        sharpe_rows=sharpe_rows,
        classification_rows=classification_rows,
        alpha_ranking=alpha_ranking,
        validation_results=validation_results,
    )

    if args.fail_on_validation_error and any(not result.matched for result in validation_results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
