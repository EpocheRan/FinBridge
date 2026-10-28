"""回测管线编排。"""

from __future__ import annotations

import json
import logging
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from backtest.application.corporate_actions import derive_approximate_actions
from backtest.application.cross_section import plan_cross_section
from backtest.application.loader import LoadedStrategy, load_strategy
from backtest.application.ports import EnginePort, OutputPort
from backtest.application.run_id import compute_run_id
from backtest.config import BacktestSettings
from backtest.domain.errors import BacktestError, ConfigError
from backtest.domain.models import BacktestResult, RunSpec
from factor_engine.definitions import INLINE_COMPUTE_REGISTRY, register_all
from factors import build_panel, required_warmup
from factors.registry import resolve_id
from strategies.base import BaseStrategy

logger = logging.getLogger("Backtest.Runner")

_CROSS_REBALANCE = {"daily", "weekly", "monthly", "every_n_bars"}


def _engine_version() -> str:
    try:
        return version("lib-pybroker")
    except PackageNotFoundError:
        return "none"


def _ensure_registry() -> None:
    try:
        register_all()
    except Exception as exc:  # 注册失败不阻断回测（stored 读按因子 id 直查）
        logger.warning("factor registry 注册失败: %s", exc)


def _canonical_id(defn) -> str:
    name = getattr(defn, "stored_column", None) or defn.name
    try:
        factor_id, _ = resolve_id(name)
        return factor_id
    except Exception:
        return str(name)


def _effective_mode(defn, global_source: str) -> str:
    """因子级 materialization 与全局 factor_source 合成有效模式。"""
    if getattr(defn, "source", "compute") != "stored":
        return "inline"
    materialization = getattr(defn, "materialization", "auto")
    if global_source == "stored":
        return "stored"
    if global_source == "compute":
        return "inline"
    return materialization


def _validate_supported(spec: RunSpec, factors, settings: BacktestSettings) -> None:
    """把“配置了但没实现”的开关挡在跑数之前，避免静默出错误结果。"""
    if spec.fill not in ("next_open", "close"):
        raise ConfigError(f"未知 fill 模式: {spec.fill!r}，可选 next_open/close")
    if spec.corporate_actions not in ("off", "approx"):
        raise ConfigError(
            f"corporate_actions 只能是 off/approx，收到 {spec.corporate_actions!r}"
        )
    if spec.allow_short:
        raise ConfigError("A 股回测不支持做空（allow_short=true）")
    if not 0.0 < float(spec.max_gross_exposure) <= 1.0:
        raise ConfigError(
            f"max_gross_exposure 必须在 (0, 1]，收到 {spec.max_gross_exposure}"
        )
    if spec.mode == "cross":
        if spec.top_n is None or int(spec.top_n) <= 0:
            raise ConfigError(f"横截面 top_n 必须 > 0，收到 {spec.top_n!r}")
        if int(spec.buffer) < 0:
            raise ConfigError(f"buffer 不能为负，收到 {spec.buffer}")
        if int(spec.every_n_bars) <= 0:
            raise ConfigError(
                f"every_n_bars 必须 > 0，收到 {spec.every_n_bars}"
            )
        if spec.rebalance not in _CROSS_REBALANCE:
            raise ConfigError(
                f"暂不支持 rebalance={spec.rebalance!r}；"
                f"可选 {sorted(_CROSS_REBALANCE)}（DRIFT 在后续接入）"
            )
        if not factors:
            raise ConfigError("横截面策略必须声明至少一个因子")
    source = str(settings.factor_source or "auto").strip().lower()
    if source not in ("auto", "compute", "stored"):
        raise ConfigError(f"未知 factor_source: {source!r}")
    compute_defs = [
        item for item in factors if getattr(item, "source", "compute") != "stored"
    ]
    if source == "stored" and compute_defs:
        raise ConfigError(
            "factor_source=stored 只支持 F.factor(...) 预计算因子；"
            "F.mom/F.ma/F.atr 等现算因子请用 auto 或 compute"
        )


def _resolve_universe(strategy: BaseStrategy) -> tuple[str, ...]:
    spec = getattr(strategy, "universe", None)
    kind = getattr(spec, "kind", "explicit")
    if kind != "explicit":
        raise ConfigError(
            f"MVP 只支持 explicit universe，收到 {kind!r}；"
            "all_a/index/file 在后续接入（先按显式股票池跑通混合架构）"
        )
    codes = tuple(sorted({str(code) for code in getattr(spec, "codes", ())}))
    if not codes:
        raise ConfigError("explicit universe 不能为空")
    return codes


def _weighting_payload(strategy: BaseStrategy) -> str:
    weighting = getattr(strategy, "weighting", None)
    if weighting is None:
        return "equal"
    params = {
        key: value for key, value in vars(weighting).items() if not key.startswith("_")
    }
    return json.dumps(
        {"type": type(weighting).__name__, "params": params},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


class BacktestRunner:
    def __init__(
        self,
        *,
        settings: BacktestSettings,
        data_source,
        engine: EnginePort,
        outputs: Sequence[OutputPort],
    ):
        self.settings = settings
        self.data_source = data_source
        self.engine = engine
        self.outputs = list(outputs)

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        strategy_path: str | Path,
        *,
        start: str | date,
        end: str | date,
        freq: str = "d",
        params: Mapping[str, Any] | None = None,
        initial_cash: float | None = None,
        seed: int | None = None,
    ) -> BacktestResult:
        start_date = pd.Timestamp(start).date()
        end_date = pd.Timestamp(end).date()
        if start_date > end_date:
            raise BacktestError(f"start={start_date} 晚于 end={end_date}")

        loaded: LoadedStrategy = load_strategy(strategy_path, params)
        strategy: BaseStrategy = loaded.strategy
        common = dict(
            start=start_date,
            end=end_date,
            freq=freq,
            initial_cash=initial_cash,
            seed=seed,
        )
        if strategy.mode == "single":
            return self._run_single(loaded, strategy, **common)
        if strategy.mode == "cross":
            return self._run_cross(loaded, strategy, **common)
        raise BacktestError(f"未知策略模式: {strategy.mode!r}")

    # ------------------------------------------------------------------ 单股
    def _run_single(
        self,
        loaded: LoadedStrategy,
        strategy: BaseStrategy,
        *,
        start: date,
        end: date,
        freq: str,
        initial_cash: float | None,
        seed: int | None,
    ) -> BacktestResult:
        symbols = tuple(str(item) for item in getattr(strategy, "symbols", ()))
        if not symbols:
            raise BacktestError("单股策略没有 symbols")
        factors = list(strategy.factors())
        spec = RunSpec(
            strategy_name=strategy.name,
            strategy_path=str(loaded.path),
            mode="single",
            symbols=symbols,
            start=start,
            end=end,
            freq=freq,
            initial_cash=self._initial_cash(initial_cash),
            params=dict(strategy.params),
            factor_source=self.settings.factor_source,
            fill=self.settings.fill,
            benchmark=self.settings.benchmark,
            rebalance=getattr(
                getattr(strategy, "rebalance", None), "value", self.settings.rebalance
            ),
            max_gross_exposure=self.settings.max_gross_exposure,
            allow_short=self.settings.allow_short,
            risk_free_rate=self.settings.risk_free_rate,
            corporate_actions=self.settings.corporate_actions,
            metrics_source="ledger",
            outputs=self.settings.outputs,
            data_source=self.settings.data,
            seed=self._seed(seed),
            rules=self.settings.rules,
        )
        _validate_supported(spec, factors, self.settings)
        data, panel, run_id, benchmark, corporate_actions = self._prepare(
            loaded, spec, factors, start, end, freq
        )
        request = self._engine_request(
            data=data,
            panel=panel,
            strategy=strategy,
            spec=spec,
            benchmark=benchmark,
            corporate_actions=corporate_actions,
        )
        return self._run_engine(request, run_id, spec)

    # ------------------------------------------------------------------ 横截面
    def _run_cross(
        self,
        loaded: LoadedStrategy,
        strategy: BaseStrategy,
        *,
        start: date,
        end: date,
        freq: str,
        initial_cash: float | None,
        seed: int | None,
    ) -> BacktestResult:
        # 混合架构：向量化排名在本层完成，PyBroker 只执行候选集。
        codes = _resolve_universe(strategy)
        top_n = int(getattr(strategy, "top_n", 0) or 0)
        buffer = int(getattr(strategy, "buffer", 0) or 0)
        every_n_bars = int(getattr(strategy, "every_n_bars", 1) or 1)
        rebalance = str(getattr(getattr(strategy, "rebalance", None), "value", "weekly"))
        weighting = _weighting_payload(strategy)
        factors = list(strategy.factors())
        spec = RunSpec(
            strategy_name=strategy.name,
            strategy_path=str(loaded.path),
            mode="cross",
            symbols=codes,
            start=start,
            end=end,
            freq=freq,
            initial_cash=self._initial_cash(initial_cash),
            params=dict(strategy.params),
            factor_source=self.settings.factor_source,
            fill=self.settings.fill,
            benchmark=self.settings.benchmark,
            rebalance=rebalance,
            max_gross_exposure=self.settings.max_gross_exposure,
            allow_short=self.settings.allow_short,
            risk_free_rate=self.settings.risk_free_rate,
            corporate_actions=self.settings.corporate_actions,
            metrics_source="ledger",
            outputs=self.settings.outputs,
            data_source=self.settings.data,
            seed=self._seed(seed),
            rules=self.settings.rules,
            top_n=top_n,
            buffer=buffer,
            every_n_bars=every_n_bars,
            weighting=weighting,
            universe="explicit",
        )
        _validate_supported(spec, factors, self.settings)
        data, panel, run_id, benchmark, corporate_actions = self._prepare(
            loaded, spec, factors, start, end, freq
        )
        plan = plan_cross_section(
            panel=panel,
            strategy=strategy,
            symbols=codes,
            start=start,
            end=end,
            calendar=data.calendar.dates,
            top_n=top_n,
            buffer=buffer,
            max_gross_exposure=float(spec.max_gross_exposure),
            rebalance=rebalance,
            every_n_bars=every_n_bars,
        )
        logger.info(
            "cross plan: universe=%d candidates=%d rebalance_days=%d top_n=%d",
            plan.universe_size,
            len(plan.candidates),
            len(plan.rebalance_dates),
            plan.top_n,
        )
        request = self._engine_request(
            data=data,
            panel=panel,
            strategy=strategy,
            spec=spec,
            cross_plan=plan,
            benchmark=benchmark,
            corporate_actions=corporate_actions,
        )
        return self._run_engine(request, run_id, spec)

    # ------------------------------------------------------------------ 公共
    def _initial_cash(self, value: float | None) -> float:
        return float(value if value is not None else self.settings.initial_cash)

    def _seed(self, value: int | None) -> int:
        return int(value if value is not None else self.settings.seed)

    def _prepare(self, loaded, spec, factors, start, end, freq):
        warmup = max(required_warmup(factors), self.settings.warmup_for(factors))
        data = self.data_source.load(
            spec.symbols, start, end, warmup=warmup, freq=freq
        )
        stored_frame = self._build_stored_frame(
            data, factors, spec, start, end, warmup
        )
        panel = build_panel(data.frame, factors, start, end, stored=stored_frame)
        benchmark = None
        if spec.benchmark:
            loader = getattr(self.data_source, "load_benchmark", None)
            if loader is not None:
                try:
                    benchmark = loader(spec.benchmark, start, end, freq=freq)
                except Exception as exc:
                    logger.warning("benchmark 加载失败 %s: %s", spec.benchmark, exc)
        run_id = compute_run_id(
            loaded=loaded,
            spec=spec,
            factors=factors,
            universe=data.universe,
            snapshot=data.snapshot,
            benchmark=benchmark,
            security_status=data.security_status,
            engine_name=self.engine.name,
            engine_version=_engine_version(),
            seed=spec.seed,
        )
        corporate_actions = (
            derive_approximate_actions(data.frame)
            if spec.corporate_actions == "approx"
            else None
        )
        logger.info(
            "run_id=%s strategy=%s mode=%s %s..%s fill=%s benchmark=%s",
            run_id,
            spec.strategy_name,
            spec.mode,
            spec.start,
            spec.end,
            spec.fill,
            spec.benchmark if benchmark is not None else "none",
        )
        return data, panel, run_id, benchmark, corporate_actions

    def _build_stored_frame(self, data, factors, spec, start, end, warmup):
        _ensure_registry()
        stored_defs = [
            item for item in factors if getattr(item, "source", "compute") == "stored"
        ]
        if not stored_defs:
            return None

        plan = [
            {
                "defn": defn,
                "factor_id": _canonical_id(defn),
                "mode": _effective_mode(defn, self.settings.factor_source),
            }
            for defn in stored_defs
        ]

        load_ids = sorted(
            {item["factor_id"] for item in plan if item["mode"] in ("stored", "auto")}
        )
        stored_frame = None
        if load_ids:
            loader = getattr(self.data_source, "load_factors", None)
            if loader is None:
                raise ConfigError("当前数据源不支持 stored 因子（缺少 load_factors）")
            load_start = pd.Timestamp(start).normalize() - pd.Timedelta(
                days=max(0, warmup) * 2 + 10
            )
            stored_frame = loader(spec.symbols, load_start.date(), end, load_ids)

        covered: set[tuple[str, str]] = set()
        if stored_frame is not None and not stored_frame.empty:
            cols = {"code", "factor_id"}
            if cols <= set(stored_frame.columns):
                covered = {
                    (str(row.code), str(row.factor_id))
                    for row in stored_frame[["code", "factor_id"]]
                    .drop_duplicates()
                    .itertuples(index=False)
                }

        codes = tuple(sorted({str(item) for item in spec.symbols}))
        extra_rows: list[dict[str, Any]] = []
        for item in plan:
            factor_id = item["factor_id"]
            missing = [code for code in codes if (code, factor_id) not in covered]
            if item["mode"] == "stored":
                if missing:
                    raise ConfigError(
                        f"因子 {factor_id} 标记 STORED，但 {start}..{end} 缺落库数据 "
                        f"（codes={missing}）。请先运行："
                        f"python -m acquisition update --code {missing[0]} "
                        f"--start {start} --end {end} --datasets factors --factor {factor_id}"
                    )
            elif item["mode"] == "inline":
                extra_rows.extend(
                    self._compute_inline(data, factor_id, codes, start)
                )
            else:  # auto
                if missing:
                    if factor_id in INLINE_COMPUTE_REGISTRY:
                        logger.warning(
                            "因子 %s stored 缺失，回退 inline: %s", factor_id, missing
                        )
                        extra_rows.extend(
                            self._compute_inline(data, factor_id, missing, start)
                        )
                    else:
                        raise ConfigError(
                            f"因子 {factor_id} 标记 AUTO 且无 inline compute，"
                            f"但 {start}..{end} 缺落库数据（codes={missing}）"
                        )

        if not extra_rows:
            return stored_frame
        extra = pd.DataFrame(extra_rows)
        if stored_frame is None or stored_frame.empty:
            return extra
        return pd.concat([stored_frame, extra], ignore_index=True)

    def _compute_inline(self, data, factor_id: str, codes, start) -> list[dict[str, Any]]:
        builtin = INLINE_COMPUTE_REGISTRY.get(factor_id)
        if builtin is None:
            raise ConfigError(
                f"因子 {factor_id} 无 inline compute 实现（materialization=inline 要求可现算）"
            )
        start_ts = pd.Timestamp(start).normalize()
        rows: list[dict[str, Any]] = []
        frame = data.frame
        for code in codes:
            group = frame[frame["symbol"].astype(str) == code].sort_values("date").copy()
            if group.empty:
                continue
            adj = pd.to_numeric(group.get("adj_factor"), errors="coerce").fillna(1.0)
            if "hfq_close" not in group.columns:
                group["hfq_close"] = group["close"] * adj
            group["hfq_high"] = group["high"] * adj
            group["hfq_low"] = group["low"] * adj
            group["hfq_open"] = group["open"] * adj
            values = builtin.compute(group)
            for date_value, value in zip(group["date"], values):
                if pd.isna(value):
                    continue
                stamp = pd.Timestamp(date_value).normalize()
                if stamp < start_ts:
                    continue
                rows.append(
                    {
                        "ts": stamp,
                        "code": code,
                        "factor_id": factor_id,
                        "value": float(value),
                    }
                )
        return rows

    def _run_engine(self, request, run_id: str, spec: RunSpec) -> BacktestResult:
        result = self.engine.run(request)
        result.run_id = run_id
        result.spec = spec
        artifacts = []
        for output in self.outputs:
            artifacts.extend(output.emit(result))
        result.meta["outputs"] = [
            {
                "kind": item.kind,
                "path": None if item.path is None else str(item.path),
                "table": item.table,
                "rows": item.rows,
            }
            for item in artifacts
        ]
        return result

    def _engine_request(
        self,
        *,
        data,
        panel,
        strategy,
        spec,
        cross_plan=None,
        benchmark=None,
        corporate_actions=None,
    ):
        from backtest.application.ports import EngineRequest

        return EngineRequest(
            spec=spec,
            data=data,
            panel=panel,
            strategy=strategy,
            universe=data.universe,
            calendar=data.calendar,
            rules=spec.rules,
            benchmark=benchmark,
            seed=spec.seed,
            cross_plan=cross_plan,
            corporate_actions=corporate_actions,
        )

    def optimize(
        self,
        strategy_path,
        *,
        start,
        end,
        grid=None,
        params=None,
        metric: str = "sharpe",
        maximize: bool = True,
        freq: str = "d",
        initial_cash: float | None = None,
        seed: int | None = None,
        max_trials: int | None = 100,
    ):
        from backtest.application.optimize import optimize

        return optimize(
            self,
            strategy_path,
            start=start,
            end=end,
            grid=grid,
            params=params,
            metric=metric,
            maximize=maximize,
            freq=freq,
            initial_cash=initial_cash,
            seed=seed,
            max_trials=max_trials,
        )

    def walkforward(
        self,
        strategy_path,
        *,
        start,
        end,
        grid=None,
        params=None,
        metric: str = "sharpe",
        maximize: bool = True,
        windows: int = 3,
        train_size: float = 0.6,
        lookahead: int = 1,
        freq: str = "d",
        initial_cash: float | None = None,
        seed: int | None = None,
        max_trials: int | None = 100,
    ):
        from backtest.application.optimize import walkforward

        return walkforward(
            self,
            strategy_path,
            start=start,
            end=end,
            grid=grid,
            params=params,
            metric=metric,
            maximize=maximize,
            windows=windows,
            train_size=train_size,
            lookahead=lookahead,
            freq=freq,
            initial_cash=initial_cash,
            seed=seed,
            max_trials=max_trials,
        )
