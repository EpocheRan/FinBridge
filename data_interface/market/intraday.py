"""分时结构分析：封板质量 / 炸板 / 尾盘竞价 / 均价线。

输入是 ``ohlcv_1m``（或 5m）的标准 DataFrame（``ts`` 索引，
列：open/high/low/close/volume/amount），输出一组可直接进因子表的标量。

口径说明
--------
- **涨跌停价**按板块规则由前收盘价推出（主板 10%、创业板/科创板 20%、北交所 30%、
  ST 5%），保留两位小数并做四舍五入到分。
- **封板**判定：某 bar 收盘价 ``>= 涨停价 - 0.005``（容忍 1 分钱内的取值误差）。
- **炸板次数**：封板状态由「封住」转为「未封住」的**跳变次数**；同一次打开只计一次。
- **尾盘竞价**：A 股 14:57–15:00 的收盘集合竞价会并入 ``15:00`` 那根 bar，
  因此 ``auction_volume`` 取最后一根且时间戳为 15:00 的 bar 的成交量。
- 新浪分时**没有 09:25 开盘竞价独立 bar**，开盘竞价成交价近似等于首根 bar 的 ``open``。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import time as dtime
from typing import Any

import pandas as pd

#: 板块涨跌幅限制
BOARD_LIMIT_PCT = {
    "MAIN": 0.10,   # 沪深主板
    "GEM": 0.20,    # 创业板
    "STAR": 0.20,   # 科创板
    "BJ": 0.30,     # 北交所
    "ST": 0.05,     # 风险警示板
}

_SEAL_TOLERANCE = 0.005
_AUCTION_TIME = dtime(15, 0)


def limit_prices(prev_close: float, board: str = "MAIN", is_st: bool = False) -> tuple[float, float]:
    """由前收盘价计算 (涨停价, 跌停价)，四舍五入到分。"""
    pct = BOARD_LIMIT_PCT["ST"] if is_st else BOARD_LIMIT_PCT.get(board.upper(), 0.10)
    up = round(float(prev_close) * (1 + pct), 2)
    down = round(float(prev_close) * (1 - pct), 2)
    return up, down


@dataclass
class SessionMetrics:
    """单日分时结构指标。"""

    bars: int
    open: float
    high: float
    low: float
    close: float
    vwap: float
    close_vs_vwap: float
    range_pct: float
    close_position: float
    limit_up: float
    sealed_bars: int
    seal_ratio: float
    first_seal_time: str | None
    last_seal_time: str | None
    break_count: int
    max_drawdown_after_seal: float
    auction_volume: int
    auction_share: float
    late_volume_share: float
    seal_quality: float


def _to_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    frame = df.copy()
    if "ts" in frame.columns:
        frame = frame.set_index("ts")
    frame.index = pd.to_datetime(frame.index)
    # 本库的时间口径：写入时把**北京时间墙钟**格式化成字符串，QuestDB 再以 UTC 标签存储
    # （见 ``fetcher._ts_to_text``）。因此读回来必须「丢掉时区标签、不做时区换算」，
    # 否则 11:12 的封板会被算成 19:12。
    if frame.index.tz is not None:
        frame.index = frame.index.tz_localize(None)
    return frame.sort_index()


def session_metrics(
    df: pd.DataFrame,
    prev_close: float,
    *,
    board: str = "MAIN",
    is_st: bool = False,
) -> SessionMetrics | None:
    """计算单日分时结构指标；数据不足一行时返回 ``None``。"""
    frame = _to_frame(df)
    if frame.empty:
        return None
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    if len(frame) < 2:
        return None

    limit_up, _ = limit_prices(prev_close, board=board, is_st=is_st)
    close = float(frame["close"].iloc[-1])
    open_ = float(frame["open"].iloc[0])
    high = float(frame["high"].max())
    low = float(frame["low"].min())
    volume = float(frame["volume"].sum())
    amount = float(frame["amount"].sum())
    vwap = amount / volume if volume else float("nan")

    sealed = frame["close"] >= limit_up - _SEAL_TOLERANCE
    sealed_bars = int(sealed.sum())
    seal_ratio = sealed_bars / len(frame)

    first_seal_time = last_seal_time = None
    if sealed_bars:
        first_ts = frame.index[sealed.to_numpy().argmax()]
        last_ts = frame.index[len(sealed) - 1 - sealed.to_numpy()[::-1].argmax()]
        first_seal_time = first_ts.strftime("%H:%M")
        last_seal_time = last_ts.strftime("%H:%M")

    # 炸板次数：封住 -> 未封住 的跳变次数
    transitions = sealed.astype(int).diff().fillna(0)
    break_count = int((transitions == -1).sum())

    # 封板后（首次封板起）相对封板价的最大回撤
    max_dd = 0.0
    if sealed_bars:
        after = frame.loc[frame.index >= frame.index[sealed.to_numpy().argmax()]]
        if not after.empty:
            max_dd = float(after["close"].min() / limit_up - 1)

    auction_volume = 0
    if len(frame) and frame.index[-1].time() == _AUCTION_TIME:
        auction_volume = int(frame["volume"].iloc[-1])
    auction_share = auction_volume / volume if volume else 0.0

    late_cut = frame.index.normalize() + pd.Timedelta(hours=14, minutes=30)
    late_volume = float(frame.loc[frame.index >= late_cut, "volume"].sum())
    late_volume_share = late_volume / volume if volume else 0.0

    close_position = (close - low) / (high - low) if high > low else 1.0
    range_pct = (high - low) / prev_close if prev_close else 0.0

    # 封板质量分（0~100）：封得早、炸得少、收得高、尾盘有资金
    quality = (
        seal_ratio * 45
        + max(0.0, 1 - break_count / 3) * 20
        + max(0.0, min(1.0, close_position)) * 20
        + min(1.0, auction_share * 10) * 10
        + min(1.0, late_volume_share * 3) * 5
    )

    return SessionMetrics(
        bars=len(frame),
        open=open_,
        high=high,
        low=low,
        close=close,
        vwap=round(vwap, 4) if vwap == vwap else float("nan"),
        close_vs_vwap=round(close / vwap - 1, 6) if vwap and vwap == vwap else float("nan"),
        range_pct=round(range_pct, 6),
        close_position=round(close_position, 4),
        limit_up=limit_up,
        sealed_bars=sealed_bars,
        seal_ratio=round(seal_ratio, 4),
        first_seal_time=first_seal_time,
        last_seal_time=last_seal_time,
        break_count=break_count,
        max_drawdown_after_seal=round(max_dd, 6),
        auction_volume=auction_volume,
        auction_share=round(auction_share, 4),
        late_volume_share=round(late_volume_share, 4),
        seal_quality=round(quality, 2),
    )


def metrics_to_dict(metrics: SessionMetrics | None) -> dict[str, Any]:
    return {} if metrics is None else asdict(metrics)
