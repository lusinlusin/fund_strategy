# Pipeline Detailed Guide

Last updated: 2026-06-02

这份文档按运行顺序解释本项目每个主要脚本做什么、关键参数是什么意思、每个函数处理什么，以及这些设计背后的动机和对结果的影响。

项目本质上是一个 **machine-learning mutual fund alpha selection strategy**：先用 CRSP mutual fund 数据构建可投资 share-class 样本，再用 FF5+MOM 计算 fund-level realized alpha，然后用机器学习模型预测下一年 alpha，最后根据预测值构建组合并回测。

## 当前 Baseline 设置

| 文件 | 参数 | 当前值 | 含义 |
|---|---:|---:|---|
| `src/sample_filter.py` | `LOAD_FILTER_MODE` | `strict` | 只有 `front_load == 0` 且 `rear_load == 0` 明确成立时才算 no-load。 |
| `src/sample_filter.py` | `DOMESTIC_EQUITY_MODE` | `strict` | 只保留严格 ED domestic-equity code list 中的基金。 |
| `src/sample_filter.py` | `PER_COM_FILL_MODE` | `backward` | `per_com` 全年缺失时，只用过去年份填补，不用未来年份。 |
| `src/features.py` | `ROLLING_WINDOW` | `36` | 用过去 36 个月估计 FF5+MOM beta。 |
| `src/config.py` | `TARGET_MODE` | `alpha` | 预测下一年 annualized realized alpha。在 config.py 定义一次，features/model/backtest 共用。 |
| `src/model.py` | `MIN_TRAIN_YEARS` | `10` | 至少有 10 年训练样本后才开始 out-of-sample prediction。 |
| `src/model.py` | `CV_FOLDS` | `5` | 用 5-fold year-based time-series CV 调参。 |
| `src/backtest.py` | `LONG_DECILE` | `0.10` | long 预测 alpha 最高的前 10%。 |
| `src/backtest.py` | `SHORT_DECILE` | `0` | long-only；不做 short leg。 |
| `src/backtest.py` | `BACKTEST_START`, `BACKTEST_END` | `None`, `None` | 使用完整可用回测区间。 |

## 推荐运行顺序

主流程：

```bash
python src/sample_filter.py
python src/features.py
python src/model.py
python src/backtest.py
```

或者直接运行：

```bash
python src/main.py
```

当前 `main.py` 会跳过数据下载，并依次运行：

```text
features.py -> model.py -> backtest.py
```

如果改了 `sample_filter.py` 中的样本筛选参数，需要从 `sample_filter.py` 开始重跑整条下游 pipeline。如果只改模型参数，通常只需要重跑 `model.py` 和 `backtest.py`。如果只改组合比例、回测区间、图表或绩效指标，只需要重跑 `backtest.py`。

# `src/main.py`

## 作用

`main.py` 是简单的 pipeline orchestrator。它不写新的研究逻辑，只是按顺序 import 各脚本并调用它们的 `main()`。

## 当前开关

```python
RUN_DATA_DOWNLOAD = False
RUN_SAMPLE_FILTER = False
RUN_FEATURES = True
RUN_MODEL = True
RUN_BACKTEST = True
```

| 开关 | 当前值 | 作用 |
|---|---:|---|
| `RUN_DATA_DOWNLOAD` | `False` | 不重新下载 raw data。 |
| `RUN_SAMPLE_FILTER` | `False` | 不重建样本。 |
| `RUN_FEATURES` | `True` | 重建 feature panel。 |
| `RUN_MODEL` | `True` | 重跑模型。 |
| `RUN_BACKTEST` | `True` | 重跑回测。 |

## 函数说明

| 函数 | 处理什么 |
|---|---|
| `import_local_module()` | 支持直接运行或 package 方式运行时正确 import 本地模块。 |
| `run_step()` | 打印时间戳、运行单个脚本、输出耗时。 |
| `main()` | 遍历 `PIPELINE_STEPS`，执行打开的步骤。 |

# `src/data_download.py`

## 作用

`data_download.py` 负责从 WRDS/CRSP 下载原始 mutual fund 数据，并从 Ken French 数据源下载 FF5+MOM 因子。

一般不需要频繁运行。只有在你想刷新 raw data、改变下载年份、或者重新清洗原始收益时，才需要运行它。

## 关键参数

| 参数 | 当前值 | 含义 | 影响 |
|---|---:|---|---|
| `START` | `1977-01-01` | 原始数据下载起点。 | 早于 1980 是为了给 36 个月 rolling alpha 留足历史数据。 |
| `END` | `2025-12-31` | 原始数据下载终点。 | 决定能否回测到 2025。 |
| `OUT` | `data/raw` | raw data 输出路径。 | 后续所有脚本都从这里或 `data/processed` 读取数据。 |

## 输出文件

| 文件 | 内容 | 后续用途 |
|---|---|---|
| `data/raw/monthly_returns.csv` | `crsp_fundno`, `caldt`, `mret`, `mtna` | 组合收益、flow、alpha 计算。 |
| `data/raw/fund_style.csv` | CRSP objective code 和 style 信息 | 判断是否 U.S. domestic equity。 |
| `data/raw/fund_fees.csv` | `exp_ratio`, `turn_ratio` | 特征构造、value added。 |
| `data/raw/fund_hdr.csv` | fund name、first offer date、index/ETF flag 等 | 样本筛选和基金年龄。 |
| `data/raw/fund_hdr_hist.csv` | 历史 fund header 和 manager 信息 | manager tenure、passive/index 判断。 |
| `data/raw/fund_summary.csv` | `per_com` 等组合持仓信息 | `per_com >= 70` 股票仓位筛选。 |
| `data/raw/ff5_mom_factors.csv` | FF5+MOM 因子和 `rf` | alpha 估计、回测 alpha 估计。 |

## 函数说明

| 函数 | 做什么 | 背后动机 |
|---|---|---|
| `clean_ret(ret)` | 清洗异常 monthly return。 | CRSP mutual fund 里可能有清盘、极端、错误收益，直接进入回测会扭曲结果。 |
| `download_ff5_factors(start=START)` | 下载 FF5 和 momentum factor，转成小数形式后保存。 | 后续 realized alpha 和 portfolio alpha 都依赖这些因子。 |
| `main()` | 连接 WRDS，下载所有 raw 表，清洗 monthly return，保存 CSV。 | 原始数据入口。 |

## `clean_ret()` 的具体规则

| 条件 | 处理 | 含义 |
|---|---:|---|
| `mret <= -1` | `mret = 0` | 小于等于 -100% 的 mutual fund return 不适合作为普通月收益使用。 |
| fund 最后一个月且 `mret > 10` | `mret = NaN` | 最后一个月超过 1000% 的收益高度可疑，可能是清盘或数据异常。 |
| `abs(mret) > 5` | `mret = NaN` | 任意月份绝对收益超过 500% 都作为异常值剔除。 |

这一步影响很大，因为 benchmark 和 selected portfolio 的 cumulative return 都直接来自 `mret`。

# `src/sample_filter.py`

## 作用

`sample_filter.py` 定义研究样本，也就是哪些 share-class-month 可以进入后续 alpha 估计、模型训练和回测。

这是整个项目最敏感的步骤之一。样本筛选变严，candidate fund 数量会下降；样本筛选变松，结果可能更接近 paper 的样本规模，但也可能引入不符合研究设计的基金。

## 输入文件

| 文件 | 用途 |
|---|---|
| `data/raw/monthly_returns.csv` | 基础月度收益和 TNA。 |
| `data/raw/front_load.csv` | front-end load 筛选。 |
| `data/raw/rear_load.csv` | back-end load 筛选。 |
| `data/raw/fund_hdr.csv` | 静态 fund header。 |
| `data/raw/fund_hdr_hist.csv` | 历史 fund header。 |
| `data/raw/fund_style.csv` | CRSP objective code。 |
| `data/raw/fund_summary.csv` | `per_com` 股票仓位。 |

## 输出文件

| 文件 | 内容 |
|---|---|
| `data/processed/sf_sample_flags.csv` | 所有主要筛选条件的 diagnostic flags。 |
| `data/processed/sf_monthly_returns.csv` | 给 `features.py` 使用的月度样本，按 `in_alpha_history` 过滤。 |

## `in_alpha_history` 和 `in_paper_sample`

这两个 flag 很重要：

| flag | 是否要求 `age_ge_36m` | 用途 | 为什么这么做 |
|---|---:|---|---|
| `in_alpha_history` | 否 | 输出到 `sf_monthly_returns.csv`，供 rolling alpha 使用。 | 计算 36 个月 rolling beta 时，需要保留基金最早 36 个月作为历史窗口。 |
| `in_paper_sample` | 是 | 最终 feature panel 和 backtest holding return。 | 真正进入模型/回测的样本需要满足 36 个月年龄要求。 |

如果在 `sample_filter.py` 阶段就删掉所有基金前 36 个月，`features.py` 就没有历史数据估计 rolling alpha。

## 参数：`LOAD_FILTER_MODE`

当前：

```python
LOAD_FILTER_MODE = 'strict'
```

这个参数决定如何判断一个 share class 是否 no-load。

代码先把 `front_load` 和 `rear_load` 按区间合并到月度数据，然后在同一个 `crsp_fundno` 内做 `ffill()`。这意味着只把过去已经知道的 load 信息向后延续，不用未来信息。

### `strict`

strict 模式下，只有同时明确满足：

```python
front_load == 0
rear_load == 0
```

才是 `no_load=True`。

| `front_load` | `rear_load` | `no_load` under `strict` | 原因 |
|---:|---:|---:|---|
| `0` | `0` | `True` | 明确知道前端和后端 load 都为 0。 |
| `>0` | `0` | `False` | 有 front load。 |
| `0` | `>0` | `False` | 有 rear load。 |
| `>0` | `>0` | `False` | 两边都有 load。 |
| `NaN` | `0` | `False` | front load 缺失，不能确认 no-load。 |
| `0` | `NaN` | `False` | rear load 缺失，不能确认 no-load。 |
| `NaN` | `NaN` | `False` | 没有证据说明是 no-load。 |

### `loose`

loose 模式下，默认认为 `no_load=True`，只有发现正 load 时才设为 False：

```python
front_load > 0 or rear_load > 0
```

| `front_load` | `rear_load` | `no_load` under `loose` | 原因 |
|---:|---:|---:|---|
| `0` | `0` | `True` | 没有正 load。 |
| `>0` | `0` | `False` | 有 front load。 |
| `0` | `>0` | `False` | 有 rear load。 |
| `>0` | `>0` | `False` | 有正 load。 |
| `NaN` | `0` | `True` | 缺失被当作没有发现 load。 |
| `0` | `NaN` | `True` | 缺失被当作没有发现 load。 |
| `NaN` | `NaN` | `True` | 缺失被当作没有发现 load。 |

### 动机和影响

`strict` 更保守，更符合 “restrict to share classes that charge no frontend or back-end loads” 的字面理解，但会因为 load 缺失而大幅减少样本。

`loose` 样本更大，可能更接近 paper 的 fund count，但它把缺失 load 默认当 no-load，这个假设更强。

## 参数：`DOMESTIC_EQUITY_MODE`

当前：

```python
DOMESTIC_EQUITY_MODE = 'strict'
```

| 模式 | 规则 | 影响 |
|---|---|---|
| `strict` | 只保留 `STRICT_DOMESTIC_EQUITY_CODES` 里的 code。 | 样本更窄、更干净。 |
| `ed_prefix` | 保留所有 `crsp_obj_cd.startswith('ED')` 的 observation。 | 样本更宽，可能包含不那么纯粹的 domestic-equity funds。 |

严格 ED list 当前包括：

```text
EDCI, EDCL, EDCM, EDCS,
EDSA, EDSC, EDSF, EDSG, EDSH, EDSI,
EDSM, EDSN, EDSR, EDSS, EDST, EDSU,
EDYB, EDYG, EDYI
```

动机是更接近 “U.S. domestic-equity mutual funds” 的定义，而不是只要 ED 开头就放进来。

## 参数：`PER_COM_FILL_MODE`

当前：

```python
PER_COM_FILL_MODE = 'backward'
```

`per_com` 表示 common stock allocation。样本要求：

```python
per_com >= 70
```

CRSP 的 December `per_com` 在 1998-2002 左右缺失严重，所以填补方式会显著影响这些年份的 candidate fund 数量。

### `backward`

主版本使用 backward-only：

| 情况 | 处理 |
|---|---|
| December `per_com` 有值 | 用 December 实际值。 |
| December 缺失但同年其他月份有值 | 用当年均值。 |
| 全年缺失但过去年份有值 | 用最近一个过去年份的 annual mean。 |
| 全年缺失且过去没有任何值 | 保持 NaN，过不了 `per_com >= 70`。 |

动机：只用历史信息，避免 look-ahead bias。

### `two_sided`

two-sided 只作为 sensitivity：

| 情况 | 处理 |
|---|---|
| 全年缺失 | 用最近年份，不管在过去还是未来。 |
| 最近的过去和未来年份距离一样 | 用两个年份均值的平均。 |

例子：如果 1997 和 2003 有值，中间年份缺失：

| 年份 | `backward` | `two_sided` |
|---:|---|---|
| 1998 | 用 1997 | 用 1997 |
| 1999 | 用 1997 | 用 1997 |
| 2000 | 用 1997 | 用 1997 和 2003 平均 |
| 2001 | 用 1997 | 用 2003 |
| 2002 | 用 1997 | 用 2003 |

`two_sided` 会用到未来信息，所以不能作为主结果。

## Passive / Index / ETF 筛选

`add_header_flags()` 通过两个来源排除 passive/index funds：

| 来源 | 规则 |
|---|---|
| `index_fund_flag` | 非缺失就认为是 index/passive。 |
| `fund_name` | 如果 `index_fund_flag` 缺失，就用名字关键词判断，如 `index`, `S&P`, `Russell`, `Nasdaq`, `Dow Jones`, `MSCI`。 |
| `et_flag` | 非缺失就认为是 ETF。 |

然后：

```python
is_active = not (is_passive or is_etf)
```

动机是保留 active mutual funds，而不是 ETF 或 passive index funds。

## 函数说明

| 函数 | 处理什么 | 背后含义 |
|---|---|---|
| `merge_interval_columns_to_monthly()` | 把区间型数据按日期合并到月度收益表。 | CRSP 很多字段不是逐月给的，而是一个 begin/end interval。 |
| `clean_per_com_dec()` | 构造 fund-year 层面的 December `per_com`，并记录 `fill_source`。 | 给 `per_com >= 70` 筛选提供年度股票仓位。 |
| `add_load_flags()` | 合并 front/rear load 并生成 `no_load`。 | 控制是否允许有申购/赎回 load 的 share class 进入样本。 |
| `add_header_flags()` | 合并 fund name、first offer date、index flag、ETF flag。 | 用于 passive/index/ETF 排除和基金年龄计算。 |
| `add_domestic_equity_flags()` | 合并 CRSP objective code 并判断 domestic equity。 | 控制基金类型。 |
| `add_age_and_tna_flags()` | 计算 `age_months`、`age_ge_36m`、first $5M TNA date。 | 避免 incubation bias 和过小基金异常。 |
| `add_per_com_flags()` | 合并年度 `per_com` 并生成 `per_com_ge_70`。 | 实现股票仓位筛选。 |
| `build_sample()` | 按顺序执行所有样本筛选。 | 样本定义的主函数。 |
| `summarize_filters()` | 输出每一步筛选后还有多少 rows 和 share classes。 | 检查样本为什么变大或变小。 |
| `main()` | 保存 `sf_sample_flags.csv` 和 `sf_monthly_returns.csv`。 | 生成 `features.py` 的输入。 |

# `src/features.py`

## 作用

`features.py` 把月度样本转成月度特征面板，核心任务是计算 fund-level realized alpha 和 supervised learning target。

## 输入文件

| 文件 | 用途 |
|---|---|
| `data/processed/sf_monthly_returns.csv` | 已筛选的月度基金样本。 |
| `data/raw/fund_fees.csv` | `exp_ratio`, `turn_ratio`。 |
| `data/raw/fund_hdr_hist.csv` | manager 信息。 |
| `data/raw/ff5_mom_factors.csv` | FF5+MOM 和 `rf`。 |

## 输出文件

| 文件 | 内容 |
|---|---|
| `data/processed/f_panel.csv` | 月度 feature panel，是 `model.py` 的输入。 |
| `data/processed/f_corr_monthly.csv` | 月度变量相关系数矩阵。 |

## 参数

| 参数 | 当前值 | 含义 |
|---|---:|---|
| `FACTORS` | `mkt_rf, smb, hml, rmw, cma, mom` | alpha 调整所用的因子。 |
| `ROLLING_WINDOW` | `36` | 用过去 36 个月估计 beta。 |

## realized alpha 怎么算

对每个 fund，在月份 `m`：

1. 用 `m-36` 到 `m-1` 的 36 个月 excess return 和 FF5+MOM 因子估计 beta。
2. 用当月 `m` 的 factor realization 和刚估出来的 beta 计算 factor-explained return。
3. 用当月 excess return 减去 factor-explained return，得到 realized alpha。

公式：

```text
alpha_m = excess_return_m - beta_hat_{m-1}' * factor_m
```

关键点：

| 对象 | 用什么数据 | 是否有未来信息 |
|---|---|---|
| beta | 截止到 `m-1` 的过去 36 个月 | 否 |
| 当月因子 | 月份 `m` 的 FF5+MOM realization | 否，这是事后计算 realized alpha 的组成部分 |
| realized alpha | 当月 excess return 减去 beta 解释部分 | 否 |

## `target_alpha` 怎么算

`target_alpha` 是下一年的 annualized realized alpha：

```text
annual_alpha_y = mean(monthly alpha in year y) * 12
target_alpha for year t = annual_alpha_{t+1}
```

所以 `year=1990` 的模型样本，target 是 1991 年的 realized alpha。它不是 1991 年 December 的单月 alpha，而是 1991 年 12 个月月度 alpha 的平均乘以 12。

## `excess_return` target 实验

`features.py` 也生成：

| 变量 | 含义 |
|---|---|
| `excess_return` | `mret - rf`（月度特征）。 |
| `target_excess_return` | 下一年 annualized excess return：`mean(year t+1 monthly excess) * 12`，attach 到 feature-year t。 |

这是给 `model.py` 的 `TARGET_MODE = 'excess_return'` 使用的。当前 baseline 是 `TARGET_MODE = 'alpha'`。

`target_excess_return` 现在和 `target_alpha` 完全同口径（都是下一年年化值），因此和 12 个月持有期对齐，也能被 `model.py` 的 December snapshot 正确取到。要跑 return 实验，只需在 `src/config.py` 里把 `TARGET_MODE` 设为 `'excess_return'`（单一来源，features/model/backtest 都从这里读取），然后从 `features.py` 开始重跑。`features.py` 里 `TARGET_MODE` 只决定最终面板按哪个 target 做 dropna；两个 target 始终都会被构造。

## 函数说明

| 函数 | 处理什么 | 背后含义 |
|---|---|---|
| `load_raw_data()` | 读取 `sf_monthly_returns.csv`、fees、manager history、factors。 | 加载构造特征所需数据。 |
| `merge_manager_history_to_monthly()` | 把 manager 信息合并到月度表。 | 生成 manager tenure。 |
| `compute_flow()` | 计算 fund flow。 | 衡量投资者申购/赎回带来的 TNA 变化。 |
| `winsorize_flow_cross_section()` | 每个月横截面 1%/99% winsorize flow。 | 减少极端 flow 对模型的影响。 |
| `winsorize_alpha_by_year()` | 按年对 monthly alpha 1%/99% winsorize。 | 目前函数保留，但在 `build_features()` 中被注释掉。 |
| `compute_rolling_alphas()` | 计算 rolling beta、monthly realized alpha、alpha t-stat、factor beta t-stat、R2。 | 生成最核心的 alpha 特征。 |
| `compute_annual_alpha()` | 把 monthly alpha 聚合成年化 annual alpha。 | 构造下一年 `target_alpha`。 |
| `compute_annual_excess_return()` | 把 monthly excess return 聚合成年化 annual excess return。 | 构造下一年 `target_excess_return`，与 alpha 同口径。 |
| `add_excess_return()` | 只加入月度 `excess_return` 特征（target 在 Step 8 构造）。 | 支持 return target 实验。 |
| `build_features()` | 按顺序构建所有特征和 target。 | `features.py` 的主逻辑。 |
| `main()` | 保存 `f_panel.csv` 和 `f_corr_monthly.csv`。 | 生成 `model.py` 输入。 |

## 主要特征含义

| 变量 | 含义 |
|---|---|
| `mtna` | Total net assets。 |
| `exp_ratio` | Expense ratio。 |
| `turn_ratio` | Turnover ratio。 |
| `age_months` | share class 年龄。 |
| `manager_tenure` | manager 任职年限。 |
| `flow` | 当月 fund flow。 |
| `flow_vol` | flow volatility。 |
| `value_added` | `(alpha + exp_ratio / 12) * tna_lag`。 |
| `alpha` | 月度 realized alpha。 |
| `alpha_tstat` | rolling regression 中 alpha intercept 的 t-stat。 |
| `r2` | rolling factor regression 的 R-squared。 |
| factor t-stats | 各 FF5+MOM beta 的 t-stat。 |

# `src/model.py`

## 作用

`model.py` 把 monthly feature panel 转成年频 fund-year panel，然后用 expanding-window walk-forward 的方式训练模型并预测下一年 fund performance。

## 输入文件

| 文件 | 内容 |
|---|---|
| `data/processed/f_panel.csv` | `features.py` 输出的月度特征面板。 |

## 输出文件

| 文件 | 内容 |
|---|---|
| `data/processed/m_predictions_alpha.csv` | alpha mode 下每个 model-year-fund 的预测。 |
| `data/processed/m_metrics_alpha.csv` | alpha mode 下每年 IC 和汇总 IC。 |
| `data/processed/m_predictions_return.csv` | return mode 下预测。 |
| `data/processed/m_metrics_return.csv` | return mode 下 IC。 |
| `data/processed/m_corr_annual.csv` | 年频变量相关系数矩阵。 |
| `data/processed/m_model_progress_*.log` | 模型训练进度日志。 |

## 参数：`TARGET_MODE`

`TARGET_MODE` 在 `src/config.py` 里定义一次，`features.py` / `model.py` /
`backtest.py` 都从那里 import，所以只需改一处即可全管线生效（无论是 `python
src/main.py` 还是单独跑某个脚本）。`model.py` 由它派生 `TARGET_COL`：

```python
# src/config.py
TARGET_MODE = 'alpha'  # 'alpha' or 'excess_return'
```

| 模式 | target | 使用的核心历史变量 | 排除的核心历史变量 | 含义 |
|---|---|---|---|---|
| `alpha` | `target_alpha` | `alpha` | `excess_return` | 预测下一年 factor-adjusted alpha。 |
| `excess_return` | `target_excess_return` | `excess_return` | `alpha` | 预测下一年 annualized excess return。 |

这样设置是为了避免 alpha 和 excess return 同时进入模型，导致 target experiment 口径混乱。

## 年频 panel 怎么构造

`prepare_annual_panel()` 做以下处理：

| 变量类型 | 年频处理 |
|---|---|
| `alpha`, `flow`, `value_added` | 月度平均乘以 12。 |
| return mode 下的 `excess_return` | 月度平均乘以 12。 |
| `flow_vol` | 当年 flow 标准差乘以 `sqrt(12)`，且至少需要 10 个 observation。 |
| 大部分静态/低频特征 | 取 December snapshot。 |
| 所有 model features | 每年横截面标准化为均值 0、标准差 1。 |
| 标准化后缺失值 | 填 0，也就是当年横截面均值。 |

动机：模型每年做横截面 ranking，所以特征需要在同一年内可比。

## walk-forward 训练逻辑

`walk_forward_predict()` 使用 expanding-window：

| 步骤 | 例子 |
|---|---|
| 至少 10 年训练样本 | 1980-1989 训练，预测 1990 feature-year。 |
| 每年重新训练 | 预测 1991 时，训练样本扩展到 1980-1990。 |
| 预测年份是 feature year | `year=1990` 代表用 1990 年特征。 |
| 组合持有年份是下一年 | `year=1990` 的预测用于 1991 年持有。 |

## 模型运行参数

| 参数 | 当前值 | 含义 | 备注 |
|---|---:|---|---|
| `MIN_TRAIN_YEARS` | `10` | 最少训练年份。 | 决定最早 prediction year。 |
| `RETRAIN_FREQ` | `1` | 预期含义是每年 retrain。 | 当前代码每年都 retrain；这个参数定义了但没有单独控制 loop。 |
| `CV_FOLDS` | `5` | CV fold 数。 | 用在 year-based time-series CV。 |
| `TEST_YEAR` | `None` | 是否只跑单一年份。 | 设成如 `2010` 可用于 debug，并只替换该年 prediction。 |

## time-series CV 怎么做

`build_year_cv_splits()` 按年份切块：

1. 找出训练样本中的 unique years。
2. 把 years 分成连续 year blocks。
3. 每个 fold 只用更早年份训练，用后面连续年份验证。

动机：金融数据有时间顺序，随机 K-fold 可能让未来年份进入调参过程，造成轻微 look-ahead。year-based CV 更保守。

## 模型和超参数

### OLS

```python
LinearRegression()
```

OLS 是线性 benchmark，不调参。它的作用是判断简单线性关系是否已经能解释大部分可预测性。

### Elastic Net

```python
ElasticNetCV(
    l1_ratio=[0.01, 0.05, 0.1, 0.3, 0.5],
    alphas=np.logspace(-7, -2, 12),
    cv=cv_splits,
    max_iter=10000,
    random_state=42,
)
```

| 参数 | 含义 | 影响 |
|---|---|---|
| `l1_ratio` | L1/L2 正则混合比例。 | 越大越像 Lasso，越容易把系数压成 0。 |
| `alphas` | 正则强度候选值。 | 越大 shrinkage 越强，可能 underfit。 |
| `cv` | year-based CV。 | 用过去到未来的 split 选参数。 |
| `max_iter` | 最大迭代次数。 | 防止优化不收敛。 |
| `random_state` | 随机种子。 | 保证可复现。 |

如果 Elastic Net 预测值大量并列或接近常数，通常说明 CV 选择了较强正则，模型认为把信号压弱可以降低 validation error。

### Random Forest

```python
RF_PARAM_GRID = {
    'n_estimators': [500, 1000],
    'max_depth': [4],
    'min_samples_leaf': [25],
    'max_features': [4],
    'bootstrap': [True],
}
```

| 参数 | 含义 | 影响 |
|---|---|---|
| `n_estimators` | 树的数量。 | 更多树更稳定，但更慢。 |
| `max_depth` | 单棵树最大深度。 | 越深越容易过拟合。 |
| `min_samples_leaf` | 叶子节点最少样本数。 | 越大预测越平滑。 |
| `max_features` | 每次 split 可选的 feature 数。 | 越小树之间差异越大，也更保守。 |
| `bootstrap` | 是否 bootstrap sample。 | Random Forest 标准设定。 |

这个 grid 比最早版本窄，因为 RF 在 expanding-window + 5-fold CV 下非常耗时。现在的 grid 是在计算可承受性和 paper 思路之间折中。

### XGBoost

```python
XGB_PARAM_GRID = {
    'n_estimators': [300, 800],
    'learning_rate': [0.01, 0.05],
    'max_depth': [2, 4],
    'min_child_weight': [5, 20],
    'subsample': [0.8],
    'colsample_bytree': [0.8],
}
```

| 参数 | 含义 | 影响 |
|---|---|---|
| `n_estimators` | boosting tree 数量。 | 更多树拟合能力更强，但更慢。 |
| `learning_rate` | 每棵树的学习步长。 | 越小越稳，但通常需要更多树。 |
| `max_depth` | 每棵树深度。 | 越深越能捕捉非线性，但更容易过拟合。 |
| `min_child_weight` | 子节点最小权重。 | 越大越保守。 |
| `subsample` | 行采样比例。 | 增加 regularization。 |
| `colsample_bytree` | 列采样比例。 | 增加 feature-level regularization。 |

## 输出中的 `year`

`m_predictions_alpha.csv` 里的 `year` 是 feature year，不是 holding year。

| prediction year | 使用的特征 | 实际持有期 |
|---:|---|---|
| 1990 | 1990 年 fund characteristics | 1991-01 到 1991-12 |
| 1991 | 1991 年 fund characteristics | 1992-01 到 1992-12 |

## IC 指标

`evaluate_predictions()` 每年计算：

```text
IC = corr(pred_alpha, realized target)
```

| 指标 | 含义 |
|---|---|
| `IC_mean` | 年度 IC 平均值。 |
| `ICIR` | IC 均值除以 IC 标准差。 |
| `IC>0` | IC 为正的年份占比。 |

IC 衡量的是横截面 ranking ability，不等于组合一定赚钱。模型可能 IC 正，但 top-decile portfolio 因为 tail noise、factor exposure、权重路径等原因 alpha 不强。

# `src/backtest.py`

## 作用

`backtest.py` 把模型预测转成组合收益，计算 cumulative return、drawdown、portfolio alpha、Sharpe 和 FF5+MOM adjusted alpha。

## 输入文件

| 文件 | 用途 |
|---|---|
| `data/processed/m_predictions_alpha.csv` | alpha mode 预测。 |
| `data/processed/m_predictions_return.csv` | return mode 预测。 |
| `data/processed/sf_monthly_returns.csv` | 被选中基金和 benchmark 的月度收益。 |
| `data/raw/ff5_mom_factors.csv` | 组合 alpha 回归所需因子。 |

## 输出文件

文件名带有策略 tag：

```text
l{LONG_DECILE * 100}_s{SHORT_DECILE * 100}_{period}
```

当前 long-only top 10%、full period 对应：

| 文件 | 内容 |
|---|---|
| `results/portfolio_returns_l10_s0_full.csv` | 每月组合收益和组合 alpha。 |
| `results/metrics_l10_s0_full.csv` | 回测指标。 |
| `results/plots/performance_l10_s0_full.png` | cumulative return、drawdown、cumulative alpha 图。 |

## 回测参数

| 参数 | 当前值 | 含义 | 影响 |
|---|---:|---|---|
| `LONG_DECILE` | `0.10` | long 预测值最高的前 10%。 | 越小越集中，越大越分散。 |
| `SHORT_DECILE` | `0` | 不做 short。 | 大于 0 时变成 top-minus-bottom long-short。 |
| `TARGET_MODE` | `alpha` | 读取 alpha prediction file。 | 从 `config.py` 读取，自动和 model 一致。 |
| `COMPOUND_ALPHA` | `True` | 用 `(1 + alpha).cumprod() - 1` 画 cumulative alpha。 | 图更像复利路径。 |
| `BACKTEST_START` | `None` | 可选回测起点。 | 可以设为 `'2006-01-01'` 做子样本。 |
| `BACKTEST_END` | `None` | 可选回测终点。 | 可以设为 `'2025-12-31'` 做子样本。 |

## long-only 和 long-short 区别

| 设置 | 策略 | benchmark |
|---|---|---|
| `SHORT_DECILE = 0` | 只 long top group。 | 加入 `equal_weight_all` 作为 long-only benchmark。 |
| `SHORT_DECILE > 0` | long top group，short bottom group。 | 不加入 `equal_weight_all`；自然 benchmark 是 0/cash 和 factor-adjusted alpha。 |

long-only 时，`cum_return > equal_weight_all` 可以说明选基跑赢等权基金 universe。long-short 时，不应该用 `equal_weight_all` 当主要 benchmark。

## 组合构建方式

`construct_portfolios()` 按年构建组合：

1. 对每个 model 和每个 prediction year，按 `pred_alpha` 排序。
2. 选出 top `LONG_DECILE`。
3. 在下一年 1 月到 12 月持有。
4. 年初等权。
5. 年内不每月重新等权。
6. 如果基金当月没有收益或消失，把它的权重分给剩余基金。
7. 下一年重新根据新 prediction 构建组合。

这个逻辑比每月重新 equal-weight 更接近 paper。

## portfolio alpha 怎么算

`construct_portfolio_alpha()` 用整个 out-of-sample 期间的组合收益估计 FF5+MOM beta：

```text
portfolio_alpha_m = portfolio_excess_return_m - beta_hat' * factor_m
```

注意：

| 变量 | 层级 | 含义 |
|---|---|---|
| `target_alpha` | fund-year | 训练模型用的下一年 realized alpha。 |
| `portfolio_alpha` | portfolio-month | 回测组合的 factor-adjusted monthly alpha。 |

两者不是同一个东西。

## 绩效指标

| 指标 | 含义 | 怎么解读 |
|---|---|---|
| `cum_return` | 几何累计收益。 | 是否赚钱。 |
| `cum_alpha` | 复利 cumulative alpha。 | 风险调整后路径。 |
| `ann_return` | 年化收益。 | 平均年化增长。 |
| `ann_vol` | 年化波动。 | 风险水平。 |
| `sharpe` | 年化 excess return / vol。 | 风险调整收益。 |
| `max_drawdown` | 最大回撤。 | 最坏路径风险。 |
| `oos_alpha_monthly` | FF5+MOM 回归截距。 | 月度 alpha。 |
| `oos_alpha_annual` | 月度 alpha 乘以 12。 | 年化 alpha。 |
| `oos_alpha_tstat` | alpha t-stat。 | alpha 的统计显著性。 |
| `n_months` | 样本月数。 | 回测长度。 |

## 函数说明

| 函数 | 处理什么 | 背后含义 |
|---|---|---|
| `load_data()` | 读取 predictions、monthly returns、factors。 | 加载回测输入。 |
| `compute_excess_return()` | 计算 excess return；long-short spread 特殊处理。 | long-only 和 long-short 的 excess return 口径不同。 |
| `strategy_label()` | 生成图表标题里的策略标签。 | 防止图名和实际参数不一致。 |
| `strategy_file_tag()` | 生成 `l10_s0` 这种文件名 tag。 | 区分不同 decile 设置。 |
| `period_file_tag()` | 生成 `full` 或 `200601_202512`。 | 区分不同回测区间。 |
| `filter_backtest_period()` | 应用回测起止日期。 | 做子样本分析。 |
| `paper_style_holding_returns()` | 计算年初等权、年内不再平衡的持有收益。 | 贴近 paper 的组合持有逻辑。 |
| `construct_portfolios()` | 根据预测值构建 long 或 long-short 组合。 | 把 prediction 转成可投资组合。 |
| `construct_portfolio_alpha()` | 计算 FF5+MOM-adjusted portfolio alpha。 | 判断组合是否有风险调整后超额收益。 |
| `compute_metrics()` | 计算收益、风险、alpha 指标。 | 回测评价核心。 |
| `compute_benchmarks()` | 构建 filtered universe 的等权 benchmark。 | long-only 策略比较对象。 |
| `plot_results()` | 画 cumulative return、drawdown、cumulative alpha。 | 观察策略路径。 |
| `main()` | 完整运行回测并保存结果。 | 回测入口。 |

# `src/run_paper_factor_pipeline.py`

## 作用

这个脚本用于 robustness/replication：不用当前下载的 `ff5_mom_factors.csv`，而是用 paper replication files 里的 Ken French factor CSV 重新跑 features、model、backtest。

动机是排查：结果和 paper 不同，到底是不是 factor data source 造成的。

## 主要参数

| 参数 | 当前值 | 含义 |
|---|---:|---|
| `TARGET_MODE` | `alpha` | 从 `config.py` 读取，决定预测 alpha 还是 excess return。 |
| `TEST_YEAR` | `None` | 是否只跑单一年份。 |
| `RUN_FEATURES` | `True` | 是否重建 paper-factor feature panel。 |
| `RUN_MODEL` | `True` | 是否用 paper-factor panel 重跑模型。 |
| `RUN_BACKTEST` | `True` | 是否用 paper factors 回测。 |
| `MATCH_AUTHOR_ROW_LIMITS` | `True` | 是否截取 factor 文件到作者使用的 row count。 |
| `FF5_MONTHLY_ROWS` | `690` | FF5 row limit。 |
| `MOM_MONTHLY_ROWS` | `1128` | Momentum row limit。 |

## 输出路径

| 路径 | 内容 |
|---|---|
| `data/processed/paper_factors/` | paper-factor 版本的 panel、predictions、metrics、logs。 |
| `results/paper_factors/` | paper-factor 版本的 backtest results。 |

## 函数说明

| 函数 | 处理什么 |
|---|---|
| `_read_ken_french_monthly_csv()` | 读取 Ken French CSV，并只保留 `YYYYMM` 月度行。 |
| `load_paper_ff5_mom_factors()` | 读取 paper FF5 和 MOM，转成 decimal returns。 |
| `build_feature_panel_with_paper_factors()` | 用 paper factors 调用 `features.build_features()`。 |
| `configure_model_module()` | 临时修改 `model.py` 的 target mode 和输出路径。 |
| `run_model_with_paper_factor_panel()` | 在 paper-factor panel 上跑 walk-forward model。 |
| `run_backtest_with_paper_factors()` | 用 paper-factor predictions 和 paper factors 回测。 |
| `main()` | 按开关运行 features/model/backtest。 |

# 如何解读结果

## 三个问题要分开

| 问题 | 主要看什么 |
|---|---|
| 策略有没有赚钱？ | `cum_return > 0`。 |
| 策略有没有跑赢 long-only benchmark？ | long-only 策略的 `cum_return > equal_weight_all`。 |
| 策略有没有风险调整后 alpha？ | `oos_alpha_annual > 0`，最好 `oos_alpha_tstat` 足够高。 |

不能只因为 positive alpha 就说一定能赚钱，也不能只因为 cumulative return 高就说有 alpha。市场上涨本身也能让 long-only portfolio 赚钱。

## long-only 结果怎么说

如果 `SHORT_DECILE = 0`：

| 证据 | 可以说什么 |
|---|---|
| `cum_return > 0` | 策略在回测中产生正收益。 |
| `cum_return > equal_weight_all` | ML 选基跑赢等权 mutual fund universe。 |
| `oos_alpha_annual > 0` | 策略产生正的 FF5+MOM-adjusted alpha。 |
| `oos_alpha_tstat` 接近或超过 2 | alpha 更有统计说服力。 |

## long-short 结果怎么说

如果 `SHORT_DECILE > 0`：

| 证据 | 可以说什么 |
|---|---|
| spread `cum_return > 0` | top group 跑赢 bottom group。 |
| `oos_alpha_annual > 0` | spread 有正的 factor-adjusted alpha。 |
| `oos_alpha_tstat` 高 | 结果更不容易只是 noise。 |

long-short 的主要 benchmark 不是 `equal_weight_all`，而是 0/cash 和 factor-adjusted alpha。

# 常见混淆点

## `year` 不是 holding year

`m_predictions_alpha.csv` 里的 `year` 是 feature year。实际持有期是下一年。

| prediction `year` | 使用的特征 | 组合持有期 |
|---:|---|---|
| 1990 | 1990 年特征 | 1991-01 到 1991-12 |
| 1991 | 1991 年特征 | 1992-01 到 1992-12 |

## `alpha`、`target_alpha`、`portfolio_alpha` 不是同一个东西

| 变量 | 层级 | 含义 |
|---|---|---|
| `alpha` | fund-month | 用过去 36 个月 beta 计算的月度 realized alpha。 |
| `target_alpha` | fund-year | 下一年 annualized realized alpha，是模型 target。 |
| `portfolio_alpha` | portfolio-month | 回测组合的 FF5+MOM-adjusted alpha。 |

## IC 和回测收益不是一回事

IC 衡量模型横截面排序能力；回测收益衡量把 top group 变成组合后的实际表现。

IC 正但 alpha 不强，可能有这些原因：

| 原因 | 解释 |
|---|---|
| top-tail noise | 整体排序有用，但 top 10% realizations 很 noisy。 |
| factor exposure | top funds 暴露在某些后续表现不好的因子上。 |
| 权重路径 | 年初等权、基金消失处理会影响组合收益。 |
| 样本定义 | load、domestic equity、per_com 筛选会改变候选池。 |
| target 和 portfolio metric 不同 | 预测的是 fund-level target，评价的是 portfolio-level return/alpha。 |

# 推荐 robustness runs

## 主 clean baseline

```python
LOAD_FILTER_MODE = 'strict'
DOMESTIC_EQUITY_MODE = 'strict'
PER_COM_FILL_MODE = 'backward'
TARGET_MODE = 'alpha'
LONG_DECILE = 0.10
SHORT_DECILE = 0
```

这是最保守、最干净、最少 look-ahead 的版本。

## load filter robustness

比较：

```python
LOAD_FILTER_MODE = 'strict'
```

和：

```python
LOAD_FILTER_MODE = 'loose'
```

这可以衡量 load missing assumption 对样本和 alpha 的影响。

## `per_com` robustness

比较：

```python
PER_COM_FILL_MODE = 'backward'
```

和：

```python
PER_COM_FILL_MODE = 'two_sided'
```

`two_sided` 不能作为主结果，只能作为 sensitivity。

## strategy robustness

| 设置 | 想回答的问题 |
|---|---|
| `LONG_DECILE = 0.05`, `SHORT_DECILE = 0` | 更集中的 long-only portfolio 是否更好？ |
| `LONG_DECILE = 0.10`, `SHORT_DECILE = 0` | 主 long-only top-decile 策略。 |
| `LONG_DECILE = 0.10`, `SHORT_DECILE = 0.10` | top-minus-bottom 是否有 spread alpha？ |
| `BACKTEST_START = '2006-01-01'` | 结果是否主要来自早期样本？ |

# 模型超参数调参说明

这一节是对 `model.py` 中 RF/XGBoost grid search 的补充说明。当前项目的目标不是单纯让 validation MSE 最小，而是希望模型的横截面排序能转化成 top-decile portfolio 的 realized alpha 和 cumulative alpha。因此，调参需要同时看：

| 指标 | 含义 | 用途 |
|---|---|---|
| validation MSE | CV 中模型预测误差。 | `GridSearchCV` 当前使用的选择标准。 |
| yearly IC | 每年 `pred_alpha` 和 `target_alpha` 的相关系数。 | 衡量横截面排序能力。 |
| ICIR | 年度 IC 的均值 / 波动。 | 衡量排序能力是否稳定。 |
| portfolio alpha | top portfolio 的 FF5+MOM-adjusted alpha。 | 判断预测信号是否能转成组合收益。 |
| cumulative alpha / return | 回测期间累计表现。 | 判断策略路径和经济意义。 |

## 为什么不能只扩大 grid

最新大 grid 的问题是运行时间很长，但策略表现没有相应改善。以 `m_model_progress_20260602_194234.log` 为例，完整模型运行约 258 分钟；RF 的 IC 略有提升，但 XGBoost 的组合表现反而变弱。

这说明盲目扩大 grid 的边际收益不高。原因是 CV 目标目前是 `neg_mean_squared_error`，它倾向于选择预测误差小、预测值保守的模型；但 mutual fund selection 更关心排序，尤其是 top-decile 的排序质量。MSE 最优不一定等于 portfolio alpha 最优。

## Random Forest 调参逻辑

最新大 grid 中，RF 经常选择：

| 参数 | 观察结果 | 含义 |
|---|---|---|
| `n_estimators` | 多数年份选 `800`，少数选 `500` 或 `1000`。 | `800` 附近基本够用，继续加树收益有限。 |
| `max_depth` | 主要选 `6` 或 `8`。 | 太浅可能 underfit，`6/8` 是主要有效区域。 |
| `max_features` | 主要选 `2` 或 `3`。 | 每个 split 不需要看太多变量，随机性有帮助。 |
| `min_samples_leaf` | `5/10/50` 都有出现。 | 需要保留不同平滑程度。 |

因此 RF 不建议继续用过大的全组合 grid，而是保留最常被选中的有效区域。

推荐下一版：

```python
RF_PARAM_GRID = {
    'n_estimators': [500, 800],
    'max_depth': [6, 8],
    'min_samples_leaf': [5, 10, 50],
    'max_features': [2, 3],
    'bootstrap': [True],
}
```

这个 grid 的目的不是追求最大搜索空间，而是在保留 RF 有效参数区域的同时控制运行时间。

## XGBoost 调参逻辑

最新大 grid 中，XGBoost 经常选择：

| 参数 | 观察结果 | 含义 |
|---|---|---|
| `learning_rate` | 所有年份都选 `0.01`。 | 模型偏向慢学习和保守预测。 |
| `max_depth` | 大多数年份选 `1`。 | 树太浅，非线性能力很弱，可能 underfit top-decile ranking。 |
| `min_child_weight` | 多数年份选 `50`。 | 分裂门槛高，模型进一步变保守。 |
| `n_estimators` | 多数年份选 `300`，部分选 `600`。 | 不需要很大的 tree count。 |
| `subsample` / `colsample_bytree` | 多数选择 `0.7/0.8`。 | 适度随机采样有帮助。 |

`max_depth=1` 对 validation MSE 可能合理，但它接近 additive stump model，排序能力可能不足。因为策略依赖 top portfolio selection，所以 XGBoost 下一版应该强制测试稍强的非线性结构。

推荐下一版：

```python
XGB_PARAM_GRID = {
    'n_estimators': [300, 600],
    'learning_rate': [0.01],
    'max_depth': [2, 3],
    'min_child_weight': [5, 20, 50],
    'subsample': [0.7, 0.8],
    'colsample_bytree': [0.7, 0.8],
}
```

这个 grid 刻意去掉 `max_depth=1`，目的是测试 XGBoost 在更强非线性条件下是否能改善 top-decile portfolio，而不是继续被 MSE-CV 推向过度保守。

## 推荐调参流程

建议按下面顺序跑：

1. 先跑上面的 focused grid。
2. 看 `m_metrics_alpha.csv` 里的 yearly IC、IC_mean、ICIR、IC>0。
3. 再跑 `backtest.py`，看 `metrics_l*_s*_*.csv` 中的 cumulative alpha、annual OOS alpha、t-stat、Sharpe 和 drawdown。
4. 如果 IC 提升但 portfolio alpha 下降，说明排序改善没有落到 top-decile，可能要调整 portfolio construction 或换调参目标。
5. 如果 MSE-CV 继续选过度保守参数，下一步应考虑用 validation IC 或 top-decile validation alpha 作为调参标准，而不是继续扩大 MSE grid。

当前最实用的判断标准是：参数不需要“最大”，而是要让模型产生稳定、可转化成组合 alpha 的横截面排序。
