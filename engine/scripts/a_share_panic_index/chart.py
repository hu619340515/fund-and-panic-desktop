"""V3盘中与近一年收盘图表。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .calendar import TradingCalendar


class ChartError(RuntimeError):
    pass


def generate_chart(
    database,
    output: Path,
    chart_type: str = "daily",
    trade_date: date | None = None,
    dpi: int = 160,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    if chart_type == "intraday":
        result = _intraday_chart(database, output, trade_date, dpi)
    elif chart_type == "daily":
        result = _daily_chart(database, output, dpi, as_of_date or date.today())
    else:
        raise ChartError(f"未知图表类型: {chart_type}")
    return result


def _intraday_chart(database, output: Path, trade_date: date | None, dpi: int) -> dict[str, Any]:
    if trade_date is None:
        latest = database.latest_realtime()
        if not latest:
            raise ChartError("数据库没有盘中指数记录")
        trade_date = date.fromisoformat(latest["trade_date"])
    rows = database.realtime_history(trade_date, limit=1000)
    if not rows:
        raise ChartError(f"目标交易日没有盘中记录: {trade_date}")
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if frame["timestamp"].isna().any() or frame["timestamp"].duplicated().any():
        raise ChartError("盘中记录时间戳为空或重复")
    components = pd.DataFrame(frame["components"].tolist())
    _render_intraday(frame, components, output, dpi)
    return {
        "type": "intraday",
        "output": str(output),
        "trade_date": trade_date.isoformat(),
        "records": len(frame),
        "as_of": frame.iloc[-1]["timestamp"].isoformat(),
        "data_policy": "database_records_only_no_fill",
    }


def _daily_chart(database, output: Path, dpi: int, as_of_date: date) -> dict[str, Any]:
    rows = database.daily_history(limit=2000)
    if not rows:
        raise ChartError("数据库没有收盘正式指数记录")
    frame = pd.DataFrame(rows)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    if frame["trade_date"].isna().any() or frame["trade_date"].duplicated().any():
        raise ChartError("收盘记录日期为空或重复")
    end = pd.Timestamp(as_of_date)
    try:
        start = end.replace(year=end.year - 1)
    except ValueError:
        start = end.replace(year=end.year - 1, day=28)
    frame = frame[
        (frame["trade_date"] >= start) & (frame["trade_date"] <= end)
    ].copy()
    if frame.empty:
        raise ChartError("近一年没有收盘正式指数记录")
    actual_dates = {item.date() for item in frame["trade_date"]}
    expected_dates = set(
        TradingCalendar().sessions_in_range(start.date(), end.date())
    )
    _render_daily(frame, output, dpi)
    return {
        "type": "daily",
        "output": str(output),
        "period": "trailing_1_year",
        "records": len(frame),
        "requested_start_date": start.date().isoformat(),
        "requested_end_date": end.date().isoformat(),
        "start_date": frame.iloc[0]["trade_date"].date().isoformat(),
        "end_date": frame.iloc[-1]["trade_date"].date().isoformat(),
        "expected_records": len(expected_dates),
        "coverage_complete": bool(expected_dates and expected_dates <= actual_dates),
        "missing_dates_filled": False,
        "data_policy": "database_records_only_no_fill",
    }


def _render_intraday(frame: pd.DataFrame, components: pd.DataFrame, output: Path, dpi: int) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
    positions = list(range(len(frame)))
    axes[0].plot(positions, frame["realtime_panic_index_raw"], label="Raw", color="#f97316", linewidth=1.5)
    axes[0].plot(positions, frame["realtime_panic_index"], label="Display", color="#dc2626", linewidth=2.2)
    axes[0].axhspan(75, 100, color="#fee2e2", alpha=0.7)
    axes[0].axhspan(60, 75, color="#ffedd5", alpha=0.7)
    axes[0].axhspan(40, 60, color="#f3f4f6", alpha=0.8)
    axes[0].set_ylim(0, 100)
    axes[0].set_ylabel("Panic Index")
    axes[0].legend(loc="upper left")
    colors = {
        "volatility": "#ef4444",
        "breadth": "#8b5cf6",
        "derivatives": "#0ea5e9",
        "liquidity": "#10b981",
    }
    for name, color in colors.items():
        if name in components:
            axes[1].plot(positions, components[name], label=name, color=color, linewidth=1.5)
    axes[1].set_ylim(0, 100)
    axes[1].set_ylabel("Components")
    axes[1].legend(loc="upper left", ncol=4)
    tick_step = max(1, len(frame) // 10)
    ticks = positions[::tick_step]
    labels = [frame.iloc[index]["timestamp"].strftime("%H:%M") for index in ticks]
    axes[1].set_xticks(ticks, labels)
    figure.suptitle(f"A-Share Realtime Panic Index {frame.iloc[-1]['trade_date']}")
    figure.tight_layout()
    figure.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(figure)


def _render_daily(frame: pd.DataFrame, output: Path, dpi: int) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(13, 6.5))
    positions = list(range(len(frame)))
    axis.plot(
        positions,
        frame["final_panic_index"],
        color="#dc2626",
        linewidth=2,
        marker="o",
        markersize=4,
    )
    axis.axhspan(75, 100, color="#fee2e2", alpha=0.7)
    axis.axhspan(60, 75, color="#ffedd5", alpha=0.7)
    axis.axhspan(40, 60, color="#f3f4f6", alpha=0.8)
    axis.axhspan(25, 40, color="#ecfdf5", alpha=0.7)
    axis.axhspan(0, 25, color="#eff6ff", alpha=0.7)
    axis.set_ylim(0, 100)
    axis.set_ylabel("Final Panic Index")
    tick_step = max(1, len(frame) // 12)
    ticks = positions[::tick_step]
    labels = [frame.iloc[index]["trade_date"].strftime("%Y-%m-%d") for index in ticks]
    axis.set_xticks(ticks, labels, rotation=30, ha="right")
    axis.set_xlim(-0.5, max(len(frame) - 0.5, 0.5))
    axis.set_title("A-Share Final Panic Index — Trailing 1-Year Actual Records")
    actual_start = frame.iloc[0]["trade_date"].strftime("%Y-%m-%d")
    actual_end = frame.iloc[-1]["trade_date"].strftime("%Y-%m-%d")
    axis.text(
        0.01,
        0.02,
        f"Actual coverage: {actual_start} to {actual_end} ({len(frame)} records); missing dates are not filled",
        transform=axis.transAxes,
        fontsize=9,
        color="#4b5563",
    )
    figure.tight_layout()
    figure.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(figure)
