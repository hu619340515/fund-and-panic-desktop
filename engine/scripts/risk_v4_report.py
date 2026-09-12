"""导出本地审计库中的公开行情、基金与模型记录；不下载、不训练、不读取持仓。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

try:  # 支持从 engine 根目录作为模块导入，也支持直接执行 scripts/risk_v4_report.py。
    from scripts.a_share_panic_index.risk_v4.providers import SYMBOLS
    from scripts.a_share_panic_index.risk_v4.service import compact
    from scripts.a_share_panic_index.risk_v4.spec import CONFIG, CONFIG_SHA256
    from scripts.a_share_panic_index.risk_v4.store import RiskStore, timestamp
except ModuleNotFoundError:  # pragma: no cover - 直接脚本执行路径
    from a_share_panic_index.risk_v4.providers import SYMBOLS
    from a_share_panic_index.risk_v4.service import compact
    from a_share_panic_index.risk_v4.spec import CONFIG, CONFIG_SHA256
    from a_share_panic_index.risk_v4.store import RiskStore, timestamp


EVENT_FIELDS = [
    "标的", "名称", "预测期限（交易日）", "跌幅阈值（%）", "时间外记录数", "阳性数", "阴性数",
    "Brier分数", "基率Brier分数", "简单波动基线Brier", "校准误差", "Brier改善区间下界", "Brier改善区间上界",
    "统计门槛通过", "PIT可核实", "允许发布", "缺项",
]


def _fingerprint(rows: list[dict[str, Any]]) -> str | None:
    """对实际写入审计库的日线作可复算摘要，不将空序列误称为数据指纹。"""
    if not rows:
        return None
    fields = ("trade_date", "open", "high", "low", "close", "amount", "source_id", "published_at", "pit_verified")
    canonical = [{field: row.get(field) for field in fields} for row in rows]
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _missing(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in (value if isinstance(value, list) else [value]):
            if item and str(item) not in result:
                result.append(str(item))
    return result


def _date_range(rows: list[dict[str, Any]], field: str = "trade_date") -> tuple[str | None, str | None]:
    dates = sorted(str(row[field]) for row in rows if row.get(field))
    return (dates[0], dates[-1]) if dates else (None, None)


def _number(value: Any, digits: int = 4) -> str:
    return f"{float(value):.{digits}f}" if isinstance(value, (int, float)) else "—"


def _brief_fund_missing(fund: dict[str, Any]) -> str:
    """Markdown 只给出复权缺口摘要；完整事件证据仍只写入 JSON。"""
    total = fund.get("复权校验", {})
    gaps = total.get("缺口", []) if isinstance(total, dict) else []
    gaps = gaps if isinstance(gaps, list) else []
    reasons = []
    for item in fund.get("缺项", []):
        text = str(item).strip()
        # `_missing` 为 JSON 诊断保留 dict 的字符串形式；Markdown 不倾倒原始事件。
        if not text or text.startswith(("{", "[")):
            continue
        if text not in reasons:
            reasons.append(text[:120] + ("…" if len(text) > 120 else ""))
    output = reasons[:2]
    if gaps:
        dates = []
        for gap in gaps:
            if isinstance(gap, dict):
                value = next((gap.get(key) for key in ("effective_date", "trade_date", "date", "period_start") if gap.get(key)), None)
                if value and str(value) not in dates:
                    dates.append(str(value))
        date_text = "、".join(dates[:3]) if dates else "日期未提供"
        output.append(f"复权缺口：{date_text}（共{len(gaps)}项）")
    return "；".join(output[:3]) or "—"


def _fund_summary(code: str, record: Any) -> dict[str, Any]:
    """仅保留公开基金代码、公开元数据与净值核验状态，绝不读取 portfolio。"""
    data = record if isinstance(record, dict) else {}
    history = data.get("history") if isinstance(data.get("history"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    rows = _as_list(history.get("history") or history.get("unit_nav"))
    start, end = _date_range(rows)
    total = history.get("total_return") if isinstance(history.get("total_return"), dict) else {}
    completeness = history.get("completeness") if isinstance(history.get("completeness"), dict) else {}
    mapping = metadata.get("benchmark_mapping") if isinstance(metadata.get("benchmark_mapping"), dict) else {}
    missing = []
    if not data:
        missing.append("尚无该公开基金的刷新记录")
    if not rows:
        missing.append("真实单位净值序列缺失")
    if not completeness.get("unit_nav_verified"):
        missing.append("单位净值未核实")
    if not total.get("available"):
        missing.extend(_as_list(total.get("diagnostics") or total.get("gaps")))
        missing.append(str(total.get("reason") or "总回报序列未完整核实"))
    if not metadata:
        missing.append("公开基金元数据缺失")
    if metadata and not metadata.get("metadata_verified"):
        missing.append("基金元数据或基准映射未核实")
    if not mapping.get("verified"):
        missing.append("未得到唯一、已核实的基准映射")
    public_metadata = {
        key: metadata.get(key) for key in (
            "code", "name", "fund_type", "asset_class", "benchmark", "benchmark_symbol",
            "benchmark_candidates", "benchmark_mapping", "metadata_verified", "pit_verified",
            "source", "published_at", "fetched_at", "diagnostics",
        ) if key in metadata
    }
    return {
        "基金代码": code,
        "公开元数据": public_metadata,
        "真实单位净值": {
            "可用": bool(history.get("available")), "记录数": len(rows), "最早日期": start, "最新日期": end,
            "来源": history.get("source"), "发布时间": history.get("published_at"), "抓取时间": history.get("fetched_at"),
            "核验": completeness, "诊断": history.get("diagnostics", []),
        },
        "复权校验": {
            "可用": bool(total.get("available")), "部分可用": bool(total.get("partial")),
            "分段数": len({row.get("segment") for row in _as_list(total.get("series"))}),
            "缺口": total.get("gaps", []), "原因": total.get("reason"),
        },
        "基准映射": mapping or {"verified": False, "reason": "缺少公开基金元数据"},
        "缺项": _missing(missing),
    }


def _auxiliary_summary(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    items: dict[str, Any] = {}
    for name in ("qvix", "futures", "breadth", "limits"):
        item = data.get(name) if isinstance(data.get(name), dict) else {}
        flags = _as_list(item.get("quality_flags"))
        missing = _missing(item.get("error"), [flag for flag in flags if flag in {"unavailable", "unverified_time"}])
        if not item:
            missing.append("尚无辅助观测记录")
        items[name] = {
            "状态": item.get("state", "missing"), "来源名称": item.get("source_id"), "上游": item.get("upstream"),
            "交易日": data.get("trade_date"), "来源时间": item.get("timestamp"), "抓取时间": data.get("fetched_at"),
            "时间核验标志": flags, "缺项": missing,
        }
    return {"请求交易日": data.get("trade_date"), "抓取时间": data.get("fetched_at"), "项目": items,
            "失败尝试": compact(_as_list(data.get("attempts")))}


def _model_summary(model: Any, source: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    payload = model if isinstance(model, dict) else {}
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    actual_fingerprint = _fingerprint(bars)
    missing = []
    if not payload:
        missing.append("尚未保存模型训练结果")
    for key, title in (("trained_through", "训练截止"), ("calibrated_through", "校准截止"),
                       ("configuration_sha256", "模型配置哈希")):
        if not artifact.get(key):
            missing.append(f"未保存{title}")
    if not actual_fingerprint:
        missing.append("没有可指纹化的真实日线")
    missing.extend(_as_list(validation.get("reasons")))
    strategy = payload.get("strategy", {})
    # compact(strategy) 会按界面传输规则剔除 grid；报告须保留开发段选择指标、
    # 基线净收益/回撤/换手率，因此逐项压缩而不丢弃候选网格。
    strategy_report = {key: compact(value) for key, value in strategy.items()} if isinstance(strategy, dict) else {}
    return {
        "训练截止": artifact.get("trained_through"), "校准截止": artifact.get("calibrated_through"),
        "模型配置哈希": artifact.get("configuration_sha256"), "实际日线数据指纹": actual_fingerprint,
        "模型保存的数据指纹": payload.get("data_fingerprint") or artifact.get("data_fingerprint"),
        "验证": compact(validation), "策略": strategy_report,
        "数据来源": compact(source), "模型缺项": _missing(missing),
    }


def _event_rows(symbol: str, name: str, validation: dict[str, Any], observation_ready: bool) -> list[dict[str, Any]]:
    rows = []
    for key, value in validation.get("events", {}).items():
        if not isinstance(value, dict):
            continue
        try:
            horizon, threshold = key.split("_", 1)
            horizon_value = int(horizon.removesuffix("d")); threshold_value = int(threshold.removesuffix("pct"))
        except (ValueError, AttributeError):
            horizon_value = None; threshold_value = None
        interval = value.get("brier_gain_95ci_block50", [None, None])
        interval = interval if isinstance(interval, list) and len(interval) == 2 else [None, None]
        rows.append({
            "标的": symbol, "名称": name, "预测期限（交易日）": horizon_value, "跌幅阈值（%）": threshold_value,
            "时间外记录数": validation.get("oos_samples", 0), "阳性数": value.get("positives"), "阴性数": value.get("negatives"),
            "Brier分数": value.get("brier"), "基率Brier分数": value.get("baseline_brier"),
            "简单波动基线Brier": value.get("simple_volatility_brier"), "校准误差": value.get("ece"),
            "Brier改善区间下界": interval[0], "Brier改善区间上界": interval[1],
            "统计门槛通过": "是" if value.get("passed") else "否", "PIT可核实": "是" if validation.get("pit_verified") else "否",
            "允许发布": "是" if key in validation.get("published_events", []) and observation_ready else "否",
            "缺项": "；".join(_missing(validation.get("reasons"))),
        })
    return rows


def export_report(database, directory):
    """写入三份公开审计报告；database 只用于读取，输出不含本地路径或用户持仓。"""
    store = RiskStore(database)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    observation = store.observation()
    audit: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    models: dict[str, Any] = {}
    for symbol, meta in SYMBOLS.items():
        bars = store.bars(symbol)
        state = store.get("state:" + symbol, {})
        source = store.get("source:" + symbol, {})
        model = store.get("model:" + symbol, {})
        validation = model.get("validation", {}) if isinstance(model, dict) else {}
        source_quality = source.get("quality", {}) if isinstance(source, dict) else {}
        missing = _missing(state.get("missing", []) if isinstance(state, dict) else [])
        if not bars:
            missing.insert(0, "缺少真实日线")
        audit.append({
            "标的": symbol, "名称": meta["name"], "记录数": len(bars),
            "最早日期": bars[0].get("trade_date") if bars else None,
            "最新日期": bars[-1].get("trade_date") if bars else None,
            "成交额非空数": sum(row.get("amount") is not None for row in bars),
            "来源": source.get("source_id") if isinstance(source, dict) else None,
            "缺失日期数": len(_as_list(source_quality.get("missing_dates"))),
            "状态": state.get("state") if isinstance(state, dict) else "missing",
            "当前恐慌": state.get("score") if isinstance(state, dict) else None,
            "过热": state.get("overheat") if isinstance(state, dict) else None,
            "时间外记录数": validation.get("oos_samples", 0), "严格PIT核实": validation.get("pit_verified", False),
            "数据指纹": _fingerprint(bars), "缺项": missing,
        })
        metrics.extend(_event_rows(symbol, meta["name"], validation, bool(observation.get("ready"))))
        models[symbol] = _model_summary(model, source if isinstance(source, dict) else {}, bars)

    watch_funds = store.get("watch_funds", [])
    public_funds = [_fund_summary(code, store.get("fund:" + code, {}))
                    for code in dict.fromkeys(code for code in _as_list(watch_funds)
                                              if isinstance(code, str) and len(code) == 6 and code.isdigit())]
    auxiliary = _auxiliary_summary(store.get("auxiliary", {}))

    with (target / "风险模型事件验收.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=EVENT_FIELDS)
        writer.writeheader()
        writer.writerows(metrics)

    code_root = Path(__file__).parent / "a_share_panic_index" / "risk_v4"
    hashes = {str(path.relative_to(Path(__file__).parent)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(code_root.glob("*.py"))}
    hashes[Path(__file__).name] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest = {
        "生成时间": timestamp(), "客户端版本": "3.0.0", "模型版本": "4.0", "数据库版本": 6,
        "观察窗口": observation, "注册标的日线审计": audit, "模型参数与验证": models,
        "公开基金": public_funds, "辅助观测": auxiliary, "源码SHA256": hashes,
        "模型配置": CONFIG, "模型配置SHA256": CONFIG_SHA256,
        "验证说明": "真实日线上的研究回放；日线逐条历史发布时间不可核验，未核验记录不应视为严格PIT或正式发布通过。",
        "隐私范围": "仅从 watch_funds 读取公开基金代码；不读取或导出 portfolio、持仓、金额、份额、在途资金及本地路径。",
    }
    (target / "模型参数与验证.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 3.0.0 实网数据与模型记录", "",
        "本报告读取本地审计库，不下载、不训练。日线和净值为已保存的实际抓取记录；缺失项按原状列出，不以空值推断为通过。",
        "", "## 注册标的日线", "", "| 标的 | 记录数 | 日期范围 | 成交额非空 | 状态 | 缺项 |",
        "|---|---:|---|---:|---|---|",
    ]
    for row in audit:
        dates = f'{row["最早日期"]} 至 {row["最新日期"]}' if row["记录数"] else "缺少日线"
        lines.append(f'| {row["名称"]}（{row["标的"]}） | {row["记录数"]} | {dates} | {row["成交额非空数"]} | {row["状态"] or "missing"} | {"；".join(row["缺项"]) or "—"} |')
    lines.extend(["", "## 默认20日、跌逾5%事件", "", "| 标的 | Brier | 基率Brier | 简单波动基线Brier | 统计门槛 | PIT可核实 |", "|---|---:|---:|---:|---|---|"])
    for row in metrics:
        if row["预测期限（交易日）"] == 20 and row["跌幅阈值（%）"] == 5:
            lines.append(f'| {row["名称"]} | {_number(row["Brier分数"])} | {_number(row["基率Brier分数"])} | {_number(row["简单波动基线Brier"])} | {row["统计门槛通过"]} | {row["PIT可核实"]} |')
    lines.extend(["", "## 公开基金净值与基准", "", "仅列出 `watch_funds` 中的公开基金代码；不包含用户持仓、金额或份额。", "",
                  "| 基金代码 | 净值日期范围 | 复权校验 | 基准映射 | 缺项 |", "|---|---|---|---|---|"])
    for fund in public_funds:
        nav = fund["真实单位净值"]; total = fund["复权校验"]; mapping = fund["基准映射"]
        dates = f'{nav["最早日期"]} 至 {nav["最新日期"]}' if nav["记录数"] else "缺少"
        mapping_text = mapping.get("symbol") or mapping.get("benchmark_symbol") or "未核实"
        lines.append(f'| {fund["基金代码"]} | {dates} | {"可用" if total["可用"] else "未完整核实"} | {mapping_text} | {_brief_fund_missing(fund)} |')
    lines.extend(["", "## 辅助观测", "", "| 项目 | 状态 | 来源名称 / 上游 | 交易日 | 来源时间核验 | 缺项 |", "|---|---|---|---|---|---|"])
    for name, item in auxiliary["项目"].items():
        source_name = " / ".join(part for part in (item["来源名称"], item["上游"]) if part) or "—"
        timing = item["来源时间"] or ("未核验" if "unverified_time" in item["时间核验标志"] else "缺少")
        lines.append(f'| {name} | {item["状态"]} | {source_name} | {item["交易日"] or "—"} | {timing} | {"；".join(item["缺项"]) or "—"} |')
    lines.extend([
        "", "## 模型与验证边界", "",
        f'真实观察记录：{observation.get("observed_days", 0)}/20 个交易日；观察窗口就绪：{"是" if observation.get("ready") else "否"}。',
        "训练截止、校准截止、配置哈希、实际日线数据指纹和模型缺项均写入《模型参数与验证.json》。事件 CSV 保持逐事件中文字段，即使没有事件也会写出表头。",
        "模型回放和发布资格依赖实际时间推进、统计门槛与 PIT 核验；报告不把缺少发布时间凭证、未保存训练产物或未完整复权的数据称作通过。", "",
    ])
    (target / "回测与实网验收.md").write_text("\n".join(lines), encoding="utf-8")
    return {"symbols": len(audit), "events": len(metrics), "public_funds": len(public_funds),
            "observed_days": observation.get("observed_days", 0),
            "files": ["风险模型事件验收.csv", "模型参数与验证.json", "回测与实网验收.md"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出本地真实数据和模型记录")
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    print(json.dumps(export_report(options.database, options.output), ensure_ascii=False))
