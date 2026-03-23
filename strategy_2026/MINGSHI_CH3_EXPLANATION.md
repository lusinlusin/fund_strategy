# Mingshi CH-3 Explanation

Mingshi CH-3 is not a direct China copy of the standard Fama-French `(Rm-Rf, SMB, HML)` model. It follows the China-specific methodology in *Size and Value in China* by Liu, Stambaugh, and Yuan.

The key idea is:

1. Keep a market factor.
2. Rebuild the size factor for the China A-share market.
3. Rebuild the value factor in a way that is more suitable for China.

## Main Differences vs Standard FF-3

The most important differences are:

- The market factor is still a market factor.
- The size and value factors are not defined in exactly the same way as the standard U.S. FF-3 model.

According to the Mingshi database description:

- Their China three-factor data is based on the "Three Factor Model Construction Methodology" from *Size and Value in China*.
- Source:
  - [Mingshi Database](https://www.mingshiim.com/database)
  - [English page](https://en.mingshiim.com/database)

According to the paper *Size and Value in China*:

- CH-3 includes:
  - a market factor
  - a China size factor
  - a China value factor

## Key Construction Differences

### 1. Size factor excludes the smallest 30% of stocks

In the China market, the smallest 30% of stocks are heavily affected by shell value and reverse-merger expectations. Because of that, they do not behave like the usual small-cap stocks in standard asset-pricing models.

So the China size factor is constructed after excluding the smallest 30% of stocks.

### 2. Value factor uses earnings-to-price instead of book-to-market

The paper shows that in the China market, `E/P` (earnings-to-price) works better than `B/M` (book-to-market) for capturing the value effect.

So the China value factor is not the same as the standard FF `HML` based on book-to-market. It is based on an earnings-price style value measure.

## Comparison with Standard FF-3

The paper also constructs a China version of the traditional Fama-French three-factor model using the usual method:

- size sorted by market capitalization
- value sorted by book-to-market
- then form `FFSMB` and `FFHML`

The paper gives the traditional FF-3 construction formulas as:

- `FFSMB = 1/3(S/H + S/M + S/L) - 1/3(B/H + B/M + B/L)`
- `FFHML = 1/2(S/H + B/H) - 1/2(S/L + B/L)`

Their conclusion is that CH-3 performs better than this traditional FF-3 construction in China.

## What Is the Risk-Free Rate in CH-3?

The `rf` used in the CH-3 construction is the **one-year deposit rate**.

The paper states that:

- the market factor is measured as the return on the top 70% of stocks by market capitalization, in excess of the one-year deposit rate
- the risk-free series is obtained from the CSMAR database on WRDS

So for this model:

- `rf` is **not** a Treasury yield
- `rf` is **not** an interbank repo rate
- `rf` is the **one-year deposit interest rate**

One practical note:

- the paper describes the factor construction in the monthly setting
- if the Mingshi database provides a daily CH-3 series, the daily `rf` is likely a dailyized or expanded version of that one-year deposit rate definition
- that last point is an inference from the paper's methodology and should be checked against the actual data file if we later use the daily series directly

## Practical Interpretation

So if we ask "How is Mingshi CH-3 calculated?", the short answer is:

1. Use the China A-share stock universe.
2. Construct a market factor.
3. Construct a China size factor after excluding the smallest 30% of stocks.
4. Construct a China value factor using earnings-to-price rather than book-to-market.

That means CH-3 is better described as:

- `Market + China Size + China Value`

rather than:

- `Rm-Rf + SMB + HML`

## Why This Matters for Our Model

This distinction is important for the current fund-style project.

If we directly replace the old factor set with Mingshi CH-3:

- the market factor is roughly comparable
- but the size and value factors no longer have exactly the same meaning
- especially the value factor is no longer a PB/BM-style `HML`

So Mingshi CH-3 may be a good ready-made China factor model for return attribution or alpha regression, but it is not automatically a one-for-one replacement for the old style-classification framework based on `SMB/HML` and the `large/small + value/growth` mapping.

## References

- [Mingshi Database](https://www.mingshiim.com/database)
- [Mingshi Database (English)](https://en.mingshiim.com/database)
- [Mingshi Research](https://en.mingshiim.com/research/)
- [Size and Value in China (PDF)](https://en.mingshiim.com/static/sizevaluechina.pdf)
- [Wharton working paper version](https://faculty.wharton.upenn.edu/wp-content/uploads/2018/03/Size-and-Value-in-China.pdf)
