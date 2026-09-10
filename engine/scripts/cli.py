#!/usr/bin/env python3
"""A股实时恐慌指数 V3 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

SCRIPT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_ROOT.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from a_share_panic_index import APP_VERSION, DB_SCHEMA_VERSION, MODEL_VERSION
from a_share_panic_index.config import Settings
from a_share_panic_index.database import Database
from a_share_panic_index.logging_utils import configure_logging


class CliArgumentError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliArgumentError(message)


def configure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("日期格式必须为 YYYY-MM-DD") from error


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="A股实时恐慌指数 V3")
    subparsers = parser.add_subparsers(dest="command")
    realtime = subparsers.add_parser("realtime", help="采集一次盘中实时指数")
    _common(realtime)
    realtime.add_argument("--date", type=parse_date)
    realtime.add_argument("--fixture")
    realtime.add_argument("--watch", action="store_true")
    realtime.add_argument("--interval", type=int, default=None)
    realtime.add_argument("--iterations", type=int, default=None, help=argparse.SUPPRESS)

    current = subparsers.add_parser("current", help="读取最新已存快照")
    _common(current)

    for name in ("daily", "finalize"):
        command = subparsers.add_parser(name, help="生成收盘正式指数")
        _common(command)
        command.add_argument("--date", type=parse_date)
        command.add_argument("--fixture")

    chart = subparsers.add_parser("chart", help="生成盘中或近一年收盘图表")
    _common(chart)
    chart.add_argument("--type", choices=("intraday", "daily"), default="daily")
    chart.add_argument("--date", type=parse_date)
    chart.add_argument("--output", "-o", default="reports/panic_index.png")
    chart.add_argument("--dpi", type=int, default=160)

    rebuild = subparsers.add_parser("rebuild", help="从真实夹具或已存快照重建正式历史")
    _common(rebuild)
    rebuild.add_argument("--fixture")

    validate = subparsers.add_parser("validate", help="验证真实已存模型历史")
    _common(validate)
    validate.add_argument("--mode", choices=("realtime", "daily"), default="realtime")
    validate.add_argument("--output", default="reports/validation")

    replay = subparsers.add_parser("replay", help="离线回放已存盘中快照")
    _common(replay)
    replay.add_argument("--date", type=parse_date, required=True)
    replay.add_argument("--speed", type=float, default=20.0)

    serve = subparsers.add_parser("serve", help="启动FastAPI和Dashboard")
    _common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--fixture")
    serve.add_argument("--no-collector", action="store_true")

    sources = subparsers.add_parser("sources", help="数据源探测与状态")
    source_commands = sources.add_subparsers(dest="sources_command")
    probe = source_commands.add_parser("probe", help="执行最小真实请求能力探测")
    _common(probe)
    probe.add_argument("--fixture")
    probe.add_argument("--output", default="reports/source_probe.json")
    status = source_commands.add_parser("status", help="读取数据源健康状态")
    _common(status)
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config")
    parser.add_argument("--database")


def parse_now(settings: Settings) -> datetime:
    value = os.environ.get("PANIC_INDEX_NOW")
    timezone = ZoneInfo(settings.get("market.timezone"))
    if not value:
        return datetime.now(timezone)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def build_runtime(args, run_id: str) -> tuple[Settings, Database, object]:
    settings = Settings(args.config)
    database = Database(
        settings.database_path(args.database), settings.backup_directory()
    )
    logging_config = settings.section("logging")
    logger = configure_logging(
        settings.resolve_path(logging_config["directory"]),
        int(logging_config["retention_days"]),
        str(logging_config["level"]),
        run_id,
    )
    return settings, database, logger


def write_json(payload: dict) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    )
    sys.stdout.flush()


def envelope(
    run_id: str,
    status: str,
    exit_code: int,
    result: dict | list | None = None,
    **extra,
) -> dict:
    return {
        "schema_version": APP_VERSION,
        "model_version": MODEL_VERSION,
        "database_schema_version": DB_SCHEMA_VERSION,
        "ok": exit_code == 0,
        "status": status,
        "exit_code": exit_code,
        "run_id": run_id,
        "generated_at": datetime.now().astimezone().isoformat(),
        "result": result,
        "retry": {
            "recommended": exit_code == 3,
            "after_seconds": 60 if exit_code == 3 else None,
        },
        "errors": [],
        **extra,
    }


def error_envelope(
    run_id: str,
    status: str,
    exit_code: int,
    error: Exception,
    result: dict | list | None = None,
    **extra,
) -> dict:
    value = envelope(run_id, status, exit_code, result, **extra)
    value["ok"] = False
    value["errors"] = [{"type": type(error).__name__, "message": str(error)}]
    return value


def run_realtime(args) -> int:
    from a_share_panic_index.pipeline.realtime import (
        IncompleteDataError,
        RealtimePipeline,
        StaleDataError,
    )
    from a_share_panic_index.providers import ProviderError

    run_id = str(uuid4())
    try:
        settings, database, logger = build_runtime(args, run_id)
        interval = int(args.interval or settings.get("realtime.refresh_seconds"))
        minimum = int(settings.get("realtime.minimum_refresh_seconds"))
        if interval < minimum:
            raise CliArgumentError(f"刷新间隔不得小于 {minimum} 秒")
        iterations = args.iterations if args.watch else 1
        completed = 0
        last_code = 0
        while iterations is None or completed < iterations:
            now = parse_now(settings)
            try:
                result, meta = RealtimePipeline(settings, database, logger).run(
                    now, args.date, args.fixture
                )
                context = meta["context"]
                if result:
                    payload = envelope(
                        run_id,
                        "success",
                        0,
                        result.to_dict(),
                        requested_date=context["requested_date"],
                        expected_trade_date=context["expected_trade_date"],
                        as_of_date=result.trade_date.isoformat(),
                        is_trading_day=context["is_trading_day"],
                        is_fresh=True,
                        quality_status=result.quality_status,
                        sources=meta["aggregate"]["sources"],
                        storage={
                            "database": str(database.path),
                            "backup": str(database.last_backup) if database.last_backup else None,
                            "idempotent_key": result.timestamp.isoformat(),
                        },
                    )
                else:
                    payload = envelope(
                        run_id,
                        meta["status"],
                        0,
                        meta.get("latest_realtime"),
                        requested_date=context["requested_date"],
                        expected_trade_date=context["expected_trade_date"],
                        as_of_date=(meta.get("latest_realtime") or {}).get("trade_date"),
                        is_trading_day=context["is_trading_day"],
                        is_fresh=False,
                        quality_status=(meta.get("latest_realtime") or {}).get("quality_status"),
                        sources={},
                        storage={"database": str(database.path)},
                    )
                last_code = 0
            except StaleDataError as error:
                payload = error_envelope(
                    run_id,
                    "stale",
                    3,
                    error,
                    result=database.latest_realtime(),
                    is_fresh=False,
                    stale_sources=error.stale_sources,
                    storage={"database": str(database.path)},
                )
                last_code = 3
            except IncompleteDataError as error:
                payload = error_envelope(
                    run_id,
                    "incomplete",
                    4,
                    error,
                    result=database.latest_realtime(),
                    is_fresh=False,
                    storage={"database": str(database.path)},
                )
                last_code = 4
            except ProviderError as error:
                payload = error_envelope(
                    run_id,
                    "incomplete",
                    4,
                    error,
                    result=database.latest_realtime(),
                    is_fresh=False,
                    storage={"database": str(database.path)},
                )
                last_code = 4
            write_json(payload)
            completed += 1
            if not args.watch or (iterations is not None and completed >= iterations):
                break
            time.sleep(0.01 if args.fixture and iterations is not None else interval)
        return last_code
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "storage_failed", 5, error))
        return 5
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_current(args) -> int:
    run_id = str(uuid4())
    try:
        _, database, _ = build_runtime(args, run_id)
        realtime = database.latest_realtime()
        daily = database.latest_daily()
        if realtime is None and daily is None:
            raise LookupError("数据库尚无V3指数记录")
        write_json(
            envelope(
                run_id,
                "current",
                0,
                {"realtime": realtime, "daily": daily},
                is_fresh=None,
                storage={"database": str(database.path)},
            )
        )
        return 0
    except (FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except LookupError as error:
        write_json(error_envelope(run_id, "no_data", 4, error))
        return 4
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "storage_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_daily(args) -> int:
    from a_share_panic_index.pipeline.daily import DailyPipeline
    from a_share_panic_index.pipeline.realtime import IncompleteDataError, StaleDataError

    run_id = str(uuid4())
    try:
        settings, database, logger = build_runtime(args, run_id)
        result, meta = DailyPipeline(settings, database, logger).run(
            parse_now(settings), args.date, args.fixture
        )
        context = meta["context"]
        if result:
            payload = envelope(
                run_id,
                "success",
                0,
                result.to_dict(),
                requested_date=context["requested_date"],
                expected_trade_date=context["expected_trade_date"],
                as_of_date=result.trade_date.isoformat(),
                is_trading_day=context["is_trading_day"],
                is_fresh=True,
                quality_status=result.quality_status,
                sources=meta["raw"].get("sources", {}),
                storage={
                    "database": str(database.path),
                    "backup": meta.get("backup"),
                },
            )
        else:
            latest = meta.get("latest_daily")
            payload = envelope(
                run_id,
                meta["status"],
                0,
                latest,
                requested_date=context["requested_date"],
                expected_trade_date=context["expected_trade_date"],
                as_of_date=(latest or {}).get("trade_date"),
                is_trading_day=context["is_trading_day"],
                is_fresh=False,
                quality_status=(latest or {}).get("quality_status"),
                storage={"database": str(database.path)},
            )
        write_json(payload)
        return 0
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except StaleDataError as error:
        write_json(
            error_envelope(
                run_id,
                "stale",
                3,
                error,
                result=database.latest_daily(),
                is_fresh=False,
                stale_sources=error.stale_sources,
                retry={"recommended": True, "after_seconds": 900},
                storage={"database": str(database.path)},
            )
        )
        return 3
    except IncompleteDataError as error:
        write_json(error_envelope(run_id, "incomplete", 4, error))
        return 4
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "storage_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_chart(args) -> int:
    from a_share_panic_index.chart import ChartError, generate_chart

    run_id = str(uuid4())
    try:
        _, database, _ = build_runtime(args, run_id)
        chart = generate_chart(
            database,
            Path(args.output),
            chart_type=args.type,
            trade_date=args.date,
            dpi=args.dpi,
        )
        write_json(envelope(run_id, "chart_success", 0, chart))
        return 0
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except ChartError as error:
        write_json(error_envelope(run_id, "chart_data_invalid", 4, error))
        return 4
    except (sqlite3.Error, OSError, ImportError) as error:
        write_json(error_envelope(run_id, "chart_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_rebuild(args) -> int:
    from a_share_panic_index.pipeline.rebuild import RebuildPipeline

    run_id = str(uuid4())
    try:
        settings, database, logger = build_runtime(args, run_id)
        result = RebuildPipeline(settings, database, logger).run(args.fixture)
        status = result["status"]
        exit_code = 0 if result.get("rebuilt_days", 0) > 0 else 4
        write_json(envelope(run_id, status, exit_code, result))
        return exit_code
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "storage_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_validate(args) -> int:
    from a_share_panic_index.validation import run_validation

    run_id = str(uuid4())
    try:
        _, database, _ = build_runtime(args, run_id)
        result = run_validation(database, args.mode, Path(args.output))
        write_json(envelope(run_id, "validation_complete", 0, result))
        return 0
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "validation_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_replay(args) -> int:
    run_id = str(uuid4())
    try:
        if args.speed <= 0:
            raise CliArgumentError("回放速度必须大于0")
        _, database, _ = build_runtime(args, run_id)
        rows = database.realtime_history(args.date, limit=5000)
        if not rows:
            raise LookupError(f"没有可回放的真实盘中快照: {args.date}")
        first = datetime.fromisoformat(rows[0]["timestamp"])
        last = datetime.fromisoformat(rows[-1]["timestamp"])
        simulated_seconds = max(0.0, (last - first).total_seconds())
        write_json(
            envelope(
                run_id,
                "replay_complete",
                0,
                {
                    "trade_date": args.date.isoformat(),
                    "records": len(rows),
                    "speed": args.speed,
                    "first_timestamp": rows[0]["timestamp"],
                    "last_timestamp": rows[-1]["timestamp"],
                    "simulated_seconds": simulated_seconds,
                    "wall_clock_seconds": simulated_seconds / args.speed,
                    "first_snapshot": rows[0],
                    "last_snapshot": rows[-1],
                    "network_access": False,
                },
            )
        )
        return 0
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except LookupError as error:
        write_json(error_envelope(run_id, "replay_no_data", 4, error))
        return 4
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "replay_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_sources_probe(args) -> int:
    from a_share_panic_index.providers import run_source_probe

    run_id = str(uuid4())
    try:
        settings, database, _ = build_runtime(args, run_id)
        result = run_source_probe(
            settings, database, Path(args.output), args.fixture
        )
        available = sum(bool(item["available"]) for item in result["results"])
        write_json(
            envelope(
                run_id,
                "source_probe_complete",
                0,
                result,
                available_providers=available,
                total_probes=len(result["results"]),
            )
        )
        return 0
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "source_probe_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_sources_status(args) -> int:
    run_id = str(uuid4())
    try:
        _, database, _ = build_runtime(args, run_id)
        result = {
            "health": database.provider_status(),
            "probe": database.probe_results(),
        }
        write_json(envelope(run_id, "source_status", 0, result))
        return 0
    except (FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except (sqlite3.Error, OSError) as error:
        write_json(error_envelope(run_id, "storage_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def run_serve(args) -> int:
    run_id = str(uuid4())
    try:
        settings, database, logger = build_runtime(args, run_id)
        from a_share_panic_index.web import create_app
        import uvicorn

        app = create_app(
            settings,
            database,
            logger,
            fixture=args.fixture,
            start_collector=not args.no_collector,
        )
        write_json(
            envelope(
                run_id,
                "server_starting",
                0,
                {
                    "dashboard": f"http://{args.host}:{args.port}",
                    "collector_enabled": not args.no_collector,
                },
            )
        )
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)
        return 0
    except (CliArgumentError, FileNotFoundError, ValueError) as error:
        write_json(error_envelope(run_id, "configuration_error", 2, error))
        return 2
    except (sqlite3.Error, OSError, ImportError) as error:
        write_json(error_envelope(run_id, "server_failed", 5, error))
        return 5
    except Exception as error:
        write_json(error_envelope(run_id, "unexpected_error", 6, error))
        return 6


def main(argv: list[str] | None = None) -> int:
    configure_utf8()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CliArgumentError as error:
        run_id = str(uuid4())
        write_json(error_envelope(run_id, "argument_error", 2, error))
        return 2
    if args.command is None:
        write_json(error_envelope(str(uuid4()), "argument_error", 2, CliArgumentError("缺少命令")))
        return 2
    if args.command == "realtime":
        return run_realtime(args)
    if args.command == "current":
        return run_current(args)
    if args.command in {"daily", "finalize"}:
        return run_daily(args)
    if args.command == "chart":
        return run_chart(args)
    if args.command == "rebuild":
        return run_rebuild(args)
    if args.command == "validate":
        return run_validate(args)
    if args.command == "replay":
        return run_replay(args)
    if args.command == "serve":
        return run_serve(args)
    if args.command == "sources":
        if args.sources_command == "probe":
            return run_sources_probe(args)
        if args.sources_command == "status":
            return run_sources_status(args)
        write_json(
            error_envelope(
                str(uuid4()), "argument_error", 2, CliArgumentError("sources缺少子命令")
            )
        )
        return 2
    write_json(error_envelope(str(uuid4()), "argument_error", 2, CliArgumentError("未知命令")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
