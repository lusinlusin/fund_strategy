import argparse
import re
from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd


ALIASES = {
    "hs300": "sh000300",
    "csi300": "sh000300",
    "hushen300": "sh000300",
    "treasury": "sh000012",
    "gov_bond": "sh000012",
    "sse_treasury": "sh000012",
}


def normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip().lower()
    if cleaned in ALIASES:
        return ALIASES[cleaned]
    if re.fullmatch(r"(sh|sz)\d{6}", cleaned):
        return cleaned
    if re.fullmatch(r"\d{6}", cleaned):
        prefix = "sz" if cleaned.startswith("399") else "sh"
        return f"{prefix}{cleaned}"
    raise ValueError(f"Unsupported index symbol: {symbol}")


def download_index(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    df = ak.stock_zh_index_daily(symbol=symbol).reset_index()
    df["date"] = pd.to_datetime(df["date"])
    start_ts = pd.to_datetime(start_date, format="%Y%m%d")
    end_ts = pd.to_datetime(end_date, format="%Y%m%d")
    df = df.loc[(df["date"] >= start_ts) & (df["date"] <= end_ts)].copy()
    if df.empty:
        raise ValueError(f"No data returned for index symbol: {symbol}")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Download China index daily history from AkShare")
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="Index codes such as sh000300, sh000012, 000300, 000012, hs300, treasury",
    )
    parser.add_argument(
        "--start-date",
        default="19700101",
        help="Start date in YYYYMMDD format, for example: 20100101",
    )
    parser.add_argument(
        "--end-date",
        default=date.today().strftime("%Y%m%d"),
        help="End date in YYYYMMDD format, for example: 20260315",
    )
    parser.add_argument(
        "--output-dir",
        default="akshare_index_data",
        help="Folder used to save CSV files",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for raw_symbol in args.symbols:
        symbol = normalize_symbol(raw_symbol)
        df = download_index(symbol=symbol, start_date=args.start_date, end_date=args.end_date)
        df.insert(0, "index_code", symbol)

        output_path = output_dir / f"{symbol}.csv"
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"Saved: {output_path}")


if __name__ == "__main__":
    main()
