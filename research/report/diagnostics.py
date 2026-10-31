"""报告自检、失效归因与因子家族定义。

这一层把「报告里的结论」变成可被程序复核的**断言**：

- ``rho_spread_identity``：分层单调性 ρ 与 Q5−Q1 的方向是否自洽；
- ``ic_spread_transmission``：Rank IC 能否按横截面离散度传导成 Q5−Q1；
- ``tradeability_interval``：用价差置信区间（而不是点估计）判断能否覆盖成本；
- ``direction_prior``：设计方向与实测 IC 是否相反（方向错，而不是「没信号」）。

所有断言只返回结构化结果，不抛异常——正常生成时写入报告，审计脚本里可升级为失败。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

#: 正态分布下五分位 Q5 与 Q1 的均值差（以横截面收益标准差为单位）。
#: φ(0.8)/0.2 = 1.395，两侧相减为 2.79；1.76 只是**单侧** 10% 尾部的均值，
#: 不能直接乘在 Q5−Q1 上。这里用 2.80，并允许在报告里覆盖。
QUINTILE_SPREAD_MULTIPLIER = 2.80

GRADE_ORDER = ("A", "B", "C", "D")


@dataclass
class Check:
    """一条可复核的断言结果。"""

    check_id: str
    title: str
    status: str  # pass / warn / fail / skip
    actual: str
    expected: str
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _status_ok(status: str) -> bool:
    return status in ("pass", "skip")


# ---------------------------------------------------------------- 基础统计
def spearman(values: Sequence[float], labels: Sequence[float] | None = None) -> float:
    """无 scipy 的 Spearman 相关；``labels`` 缺省为等距组序号 1..n。"""
    arr = np.asarray(values, dtype="float64")
    ok = np.isfinite(arr)
    arr = arr[ok]
    if arr.size < 3:
        return float("nan")
    if labels is None:
        lab = np.arange(1, arr.size + 1, dtype="float64")
    else:
        lab = np.asarray(labels, dtype="float64")[ok]
    if np.unique(arr).size < 2 or np.unique(lab).size < 2:
        return float("nan")
    rho = float(pd.Series(arr).rank().corr(pd.Series(lab).rank()))
    return rho


def spearman_p(rho: float, n: int) -> float:
    """Spearman ρ 的 t 近似双尾 p 值（n 为组数，仅作量级参考）。"""
    if not np.isfinite(rho) or n < 3:
        return float("nan")
    rho = max(-0.999999, min(0.999999, rho))
    from math import erfc, sqrt

    t = rho * sqrt((n - 2) / (1 - rho * rho))
    # t 分布尾概率用正态近似；n=5 时只用于提示，不作显著性结论
    return float(erfc(abs(t) / sqrt(2)))


def group_monotonicity(means: dict[str, float] | pd.Series) -> tuple[float, float]:
    """按 Q1..Qn 顺序计算有符号单调性 ρ 与近似 p 值。"""
    if isinstance(means, dict):
        keys = sorted(means, key=lambda k: int(re.sub(r"\D", "", k) or 0))
        values = [means[k] for k in keys]
    else:
        values = list(means.to_numpy())
    rho = spearman(values)
    return rho, spearman_p(rho, len(values))


def spread_stats(series: pd.Series) -> dict:
    """期均值、标准误、t 与 95% CI（价差用每期收益，不用年化）。"""
    clean = series.replace([np.inf, -np.inf], np.nan).dropna()
    n = len(clean)
    if n < 3:
        return {
            "mean": float(clean.mean()) if n else float("nan"),
            "se": float("nan"),
            "t": float("nan"),
            "ci_lo": float("nan"),
            "ci_hi": float("nan"),
            "n": int(n),
        }
    mean = float(clean.mean())
    se = float(clean.std(ddof=1) / np.sqrt(n))
    return {
        "mean": mean,
        "se": se,
        "t": float(mean / se) if se > 0 else float("nan"),
        "ci_lo": float(mean - 1.96 * se),
        "ci_hi": float(mean + 1.96 * se),
        "n": int(n),
    }


# ---------------------------------------------------------------- 断言
def rho_spread_identity(rho: float, spread: float, groups: int = 5) -> Check:
    """ρ 与 Q5−Q1 的方向自洽性。

    严格可证的边界：``ρ ≥ 0.8``（n=5）时 Σd²≤4，排列只允许一次相邻交换，
    此时 Q5 必然高于 Q1；反之 ``ρ ≤ −0.8`` 时 Q5 必然低于 Q1。
    中间区间（0<ρ<0.8）**可以**出现 Q5<Q1（例如组均值排序 [3,1,4,5,2] 的 ρ=0.2），
    因此那里只报警、不判失败——把 ``ρ>0 ⇒ Q5>Q1`` 当恒等式会误杀非单调因子。
    """
    if not np.isfinite(rho) or not np.isfinite(spread):
        return Check("rho_spread", "ρ 与 Q5−Q1 恒等式", "skip", "—", "—", "样本不足")
    actual = f"ρ={rho:+.2f}，Q5−Q1={spread * 100:+.2f}%/期"
    if rho >= 0.80 and spread <= 0:
        return Check(
            "rho_spread", "ρ 与 Q5−Q1 恒等式", "fail", actual, "ρ≥0.8 时 Q5−Q1 必须 >0",
            "n=5 且 ρ≥0.8 只允许一次相邻交换，Q5 不可能低于 Q1，属计算/对齐错误。",
        )
    if rho <= -0.80 and spread >= 0:
        return Check(
            "rho_spread", "ρ 与 Q5−Q1 恒等式", "fail", actual, "ρ≤−0.8 时 Q5−Q1 必须 <0",
            "方向定义与分组排序相反，先查 sig 是否被重复取负。",
        )
    if rho > 0 and spread <= 0:
        return Check(
            "rho_spread", "ρ 与 Q5−Q1 恒等式", "warn", actual, "ρ>0 但 Q5≤Q1",
            "中间组非单调时可出现（如 [3,1,4,5,2]），需看 Q2–Q4 是否贡献了正相关；"
            "不能直接用「ρ>0 则价差为正」做硬断言。",
        )
    return Check("rho_spread", "ρ 与 Q5−Q1 恒等式", "pass", actual, "同号", "")


def ic_spread_transmission(
    ic: float,
    sigma: float,
    spread_gross: float,
    *,
    multiplier: float = QUINTILE_SPREAD_MULTIPLIER,
) -> Check:
    """IC 能否按理论系数传导成 Q5−Q1（毛价差，期均值）。

    期望价差 = IC × 横截面收益标准差 × 2.80（五分位正态近似）。
    实测/期望 <0 或 >2 时报警：前者是符号/对齐错误，后者说明价差被少数极值主导。
    """
    if not np.isfinite(ic) or not np.isfinite(sigma) or sigma <= 0 or not np.isfinite(spread_gross):
        return Check("ic_transmission", "IC→Q5−Q1 传导", "skip", "—", "—", "数据不足")
    expected = ic * sigma * multiplier
    if abs(expected) < 1e-8:
        return Check("ic_transmission", "IC→Q5−Q1 传导", "warn",
                     f"IC={ic:+.3f}，期望价差≈0", "—", "IC 接近 0，传导检验无意义")
    ratio = spread_gross / expected
    actual = (
        f"实测 {spread_gross * 100:+.2f}%/期 vs 期望 {expected * 100:+.2f}%/期"
        f"（IC {ic:+.3f}×σ {sigma:.1%}×{multiplier:.2f}），比值 {ratio:+.2f}"
    )
    if ratio < 0 or ratio > 2:
        return Check(
            "ic_transmission", "IC→Q5−Q1 传导", "warn", actual, "0 < 实测/期望 < 2",
            "符号相反说明 IC 与分组收益不是同一口径（重叠期/极值/复利）；"
            "比值 >2 说明价差被少数极端收益放大。此时不能只用 IC 判断因子好坏。",
        )
    return Check("ic_transmission", "IC→Q5−Q1 传导", "pass", actual, "0 < 实测/期望 < 2", "")


def tradeability_interval(spread_gross: dict, cost: float) -> Check:
    """用毛价差的 95% CI 与单期成本比较：上界<成本→触发；下界>成本→通过；否则边界。"""
    mean = float(spread_gross.get("mean", np.nan))
    lo = float(spread_gross.get("ci_lo", np.nan))
    hi = float(spread_gross.get("ci_hi", np.nan))
    if not np.isfinite(mean):
        return Check("tradeability", "不可交易（价差 vs 成本）", "skip", "—", f"成本 {cost:.2%}/期", "无价差")
    actual = f"毛价差 {mean * 100:+.2f}%/期（95% CI {lo * 100:+.2f}% ~ {hi * 100:+.2f}%），成本 {cost * 100:.2f}%"
    if np.isfinite(hi) and hi < cost:
        return Check("tradeability", "不可交易（价差 vs 成本）", "fail", actual,
                     "价差上界 ≥ 成本", "即使按置信区间上界也覆盖不了成本，触发「不可交易」。")
    if np.isfinite(lo) and lo > cost:
        return Check("tradeability", "不可交易（价差 vs 成本）", "pass", actual,
                     "价差下界 > 成本", "按置信区间下界仍能覆盖成本。")
    return Check("tradeability", "不可交易（价差 vs 成本）", "warn", actual,
                 "下界>成本 或 上界<成本", "区间跨成本，无法判断，判为边界而不是通过。")


def direction_prior(ic: float, t: float, direction: int) -> Check:
    """设计方向与实测 IC 是否相反。

    不自动翻转：同一样本上按 IC 符号选方向等价于样本内择向，会把随机因子的 IC
    系统性抬成正数（原报告「4/4 强因子 IC>0」即由此而来）。
    """
    if not np.isfinite(ic):
        return Check("direction", "设计方向 vs 实测 IC", "skip", "—", "—", "无 IC")
    actual = f"设计方向 {direction:+d}，IC {ic:+.3f}，独立 t {t:+.1f}"
    if ic < -0.010 and (not np.isfinite(t) or t < -2):
        return Check("direction", "设计方向 vs 实测 IC", "fail", actual, "IC 与设计方向同号",
                     "方向与先验相反；只有在独立样本/经济逻辑确认后才能反用，不能在本样本内翻转。")
    return Check("direction", "设计方向 vs 实测 IC", "pass", actual, "IC 与设计方向同号", "")


def reproduction_check(left: dict, right: dict, keys: Iterable[str]) -> Check:
    """同输入两次运行逐点相等（浮点容差 1e-12）。"""
    key_list = list(keys)
    diffs = []
    for key in key_list:
        a, b = left.get(key), right.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if not (np.isfinite(a) and np.isfinite(b)):
                if np.isfinite(a) != np.isfinite(b):
                    diffs.append(key)
            elif abs(float(a) - float(b)) > 1e-12:
                diffs.append(key)
        elif a != b:
            diffs.append(key)
    if diffs:
        return Check("reproduction", "同输入复现", "fail", f"{len(diffs)} 个字段不一致：{', '.join(diffs[:5])}",
                     "逐点相等", "同输入两次运行应完全一致；不一致说明有未固定随机源或全局状态。")
    return Check("reproduction", "同输入复现", "pass", f"{len(key_list)} 个字段逐点相等",
                 "逐点相等", "")


# ---------------------------------------------------------------- 归因
def attribute_failure(report) -> dict:
    """给 D 级因子一个可复核的失效归因，而不是笼统的「无效」。"""
    stats = report.ic_stats
    h = 5 if 5 in stats.index else (stats.index[0] if len(stats) else None)
    ic = float(stats.loc[h, "mean"]) if h is not None else float("nan")
    t = float(stats.loc[h, "t_indep"]) if h is not None else float("nan")
    if not np.isfinite(t) and h is not None:
        t = float(stats.loc[h, "t"])
    checks = {c.check_id: c for c in (report.diagnostics or [])}
    if report.coverage < 0.50 or report.n_dates < 120:
        return {"label": "数据不足", "reason": f"覆盖率 {report.coverage:.0%}，样本 {report.n_dates} 日"}
    if checks.get("reproduction") and checks["reproduction"].status == "fail":
        return {"label": "复现失败", "reason": checks["reproduction"].actual}
    if checks.get("direction") and checks["direction"].status == "fail":
        return {"label": "方向错", "reason": checks["direction"].actual}
    trans = checks.get("ic_transmission")
    rho = checks.get("rho_spread")
    if trans and trans.status == "warn" and "比值 -" in trans.actual:
        return {"label": "传导断裂", "reason": trans.actual}
    if rho and rho.status == "fail":
        return {"label": "算错", "reason": rho.actual}
    if not np.isfinite(ic) or abs(ic) < 0.005 or (np.isfinite(t) and abs(t) < 1.5):
        return {"label": "无信号(IC→0)", "reason": f"IC {ic:+.3f}，独立 t {t:+.1f}"}
    return {"label": "证据不足", "reason": f"IC {ic:+.3f} 未通过稳定性/价差门槛"}


def overall_status(checks: Sequence[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "pass"


# ---------------------------------------------------------------- 家族
#: 族名 → 中文标签；族用于「局部评级」与相关性聚类，不用于跨族比较。
_FAMILY_RULES: tuple[tuple[str, str, str], ...] = (
    (r"^liquidity\.turnover_std_bias", "liquidity.turnover_std_bias", "换手波动偏离"),
    (r"^liquidity\.turnover_bias", "liquidity.turnover_bias", "换手偏离"),
    (r"^liquidity\.turnover_std", "liquidity.turnover_std", "换手波动"),
    (r"^liquidity\.turnover_ma", "liquidity.turnover_ma", "换手均值"),
    (r"^liquidity\.turnover_to_mv", "liquidity.turnover_to_mv", "换手/市值"),
    (r"^liquidity\.amount_ma", "liquidity.amount_ma", "成交额均值"),
    (r"^liquidity\.absret_amount", "liquidity.absret_amount", "收益×成交额"),
    (r"^liquidity\.vol_alpha", "liquidity.vol_alpha", "成交量 alpha"),
    (r"^momentum\.alpha", "momentum.alpha", "动量 alpha"),
    (r"^momentum\.ret|^reversal\.ret", "momentum.ret", "区间收益"),
    (r"^momentum\.macd|^momentum\.dif|^momentum\.dea", "momentum.macd", "MACD 族"),
    (r"^momentum\.rsrs", "momentum.rsrs", "RSRS"),
    (r"^momentum\.ma_", "momentum.ma", "均线位置"),
    (r"^momentum\.updown", "momentum.updown", "连涨连跌"),
    (r"^momentum\.intraday", "momentum.intraday", "日内位置"),
    (r"^reversal\.rsi", "reversal.rsi", "RSI"),
    (r"^reversal\.round_gap", "reversal.round_gap", "跳空"),
    (r"^risk\.beta", "risk.beta", "Beta"),
    (r"^risk\.ivol|^risk\.vol_beta", "risk.ivol", "特质波动"),
    (r"^risk\.vol_", "risk.vol", "波动率"),
    (r"^risk\.high_low", "risk.high_low", "高低价"),
    (r"^risk\.sharpe", "risk.sharpe", "滚动夏普"),
    (r"^risk\.band_break", "risk.band_break", "带宽突破"),
    (r"^risk\.log_price", "risk.log_price", "对数价格"),
    (r"^quality\.delta_", "quality.delta", "质量变化"),
    (r"^quality\.(roe|roa|eps|parent_roe)", "quality.profit", "盈利水平"),
    (r"^quality\.(gross|net|operating)_margin", "quality.margin", "利润率"),
    (r"^quality\.(asset|fixed|equity|inventory|receivable)_turnover", "quality.turnover", "周转率"),
    (r"^quality\.(debt|de|fin_leverage|mv_leverage)", "quality.leverage", "杠杆"),
    (r"^quality\.(current|quick|cash)_ratio", "quality.liquidity", "流动性比率"),
    (r"^quality\.(interest|cash_interest)_cover", "quality.cover", "利息保障"),
    (r"^quality\.", "quality", "其它质量"),
    (r"^value\.", "value", "估值"),
    (r"^size\.", "size", "规模"),
)


def family_of(factor_id: str) -> str:
    key = str(factor_id)
    for pattern, family, _label in _FAMILY_RULES:
        if re.search(pattern, key):
            return family
    return key.split(".", 1)[0]


def family_label(family: str) -> str:
    for _pattern, name, label in _FAMILY_RULES:
        if name == family:
            return label
    return family


def variant_of(factor_id: str) -> str:
    """同公式不同窗口/基准的「变体族」，用于同族价差一致性检查。

    比 ``family_of`` 更窄：``value.ep`` 与 ``value.bp`` 不是变体，
    但 ``turnover_bias_21_252`` 与 ``42_504`` 是；``beta_*_csi300`` 与 ``_sh`` 也是。
    """
    key = str(factor_id)
    category, _, short = key.partition(".")
    s = re.sub(r"(_\d+d?)+$", "", short)
    s = re.sub(r"_(csi300|sh|sz|all)$", "", s)
    s = re.sub(r"_(ttm|q|y)$", "", s)
    if s in ("dif", "dea"):
        s = "macd"
    return f"{category}.{s}" if category else s


def family_table(factor_ids: Iterable[str]) -> pd.DataFrame:
    rows = [{"factor_id": f, "family": family_of(f), "family_label": family_label(family_of(f))}
            for f in factor_ids]
    return pd.DataFrame(rows)


def grade_distribution(grades: Iterable[str]) -> dict:
    counts = {g: 0 for g in GRADE_ORDER}
    for g in grades:
        if g in counts:
            counts[g] += 1
    total = sum(counts.values())
    return {
        "counts": counts,
        "shares": {g: (counts[g] / total if total else 0.0) for g in GRADE_ORDER},
        "total": total,
    }


def retention_note(ratio: float, max_corr: float, ic_raw: float = float("nan")) -> str:
    """残差保留率的统一措辞；负/负比值与负保留率都不能输出无意义百分比。"""
    if np.isfinite(ic_raw) and ic_raw <= 0:
        return "原始 IC 为负（方向未通过），残差 IC 无经济含义，增量性 0 分。"
    if not np.isfinite(ratio):
        return "残差 IC 不可计算，增量性按中性分处理。"
    if ratio < 0:
        return (
            "残差为负：剔除同类因子后 IC 反向，增量性 0 分"
            f"（与自身相关 {max_corr:.2f}，属重复因子而非独立 alpha）。"
        )
    if max_corr >= 0.95:
        return f"与同类因子相关 {max_corr:.2f}，基本是同一因子，增量性 0 分。"
    return f"残差 IC 保留 {ratio:.0%}（同类最高相关 {max_corr:.2f}）。"


__all__ = [
    "Check",
    "GRADE_ORDER",
    "QUINTILE_SPREAD_MULTIPLIER",
    "attribute_failure",
    "direction_prior",
    "family_label",
    "family_of",
    "family_table",
    "grade_distribution",
    "group_monotonicity",
    "ic_spread_transmission",
    "overall_status",
    "reproduction_check",
    "retention_note",
    "rho_spread_identity",
    "spearman",
    "spearman_p",
    "spread_stats",
    "tradeability_interval",
]
