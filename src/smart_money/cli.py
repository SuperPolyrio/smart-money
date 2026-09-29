"""Run wallet screening or analyze an explicit trade evidence packet."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from smart_money.bootstrap.paths import load_environment
from smart_money.infrastructure.wallet_storage import migrate_watchlist
from smart_money.research.engine import MasEngine
from smart_money.research.models import AnalysisRequest
from smart_money.wallets.discovery import LEADERBOARD_SECTORS
from smart_money.watchlist import refresh_watchlist, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="聪明钱钱包与交易证据分析")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="分析交易证据")
    analyze.add_argument("--input", required=True, type=Path, help="包含 candidate、context、evidence 的 JSON 文件")
    analyze.add_argument("--output", type=Path, help="结果 JSON；省略时输出到标准输出")
    analyze.add_argument("--offline", action="store_true", help="仅分析输入证据，不调用模型或外部来源")
    watchlist = commands.add_parser("watchlist", help="发现、核查并维护唯一钱包观察名单")
    watchlist.add_argument("--migrate-storage", type=Path, help="将冻结的旧名单流式迁往隔离的 --output；不删除输入")
    watchlist.add_argument("--input", type=Path, help="本地候选、钱包映射及历史事实 JSON")
    watchlist.add_argument("--output", type=Path, default=Path("var/wallet_acceptance.json"), help="唯一名单文件")
    watchlist.add_argument(
        "--repair", action="store_true", help="按已保存的钱包与截止时间重试缺失事实，复用成功的官方快照"
    )
    watchlist.add_argument("--wallet", dest="repair_wallet", help="仅与 --repair 使用，限定一个已保存的实际交易钱包")
    watchlist.add_argument("--offline", action="store_true", help="只读取显式输入中的榜单及历史快照")
    watchlist.add_argument("--as-of", help="统一评价截止时间；离线时默认使用输入的 as_of")
    watchlist.add_argument("--categories", nargs="+", choices=list(LEADERBOARD_SECTORS), help="默认读取所有支持板块")
    watchlist.add_argument("--due", action="store_true", help="执行一次到期的每日更新、结算变更及失败重试")
    watchlist.add_argument("--watch", action="store_true", help="持续检查每日更新和正式结算变化")
    watchlist.add_argument("--poll-seconds", type=float, default=300, help="维护检查间隔，默认 300 秒")
    monitor = commands.add_parser("monitor", help="消费唯一名单，监听确认成交并交给现有 MAS")
    monitor.add_argument("--watchlist", required=True, type=Path, help="已有唯一名单文件")
    monitor.add_argument(
        "--output", type=Path, default=Path("var/wallet_monitor.json"), help="游标、仓位、观察与研究状态"
    )
    monitor.add_argument("--watch", action="store_true", help="持续扫描；默认仅处理一批区块及一条待研究观察")
    monitor.add_argument("--offline-mas", action="store_true", help="监听仍读取节点；MAS 不调用模型或外部来源")
    monitor.add_argument("--block-batch-size", type=int, default=100)
    monitor.add_argument("--poll-seconds", type=float, default=5)
    monitor.add_argument("--max-block-age-seconds", type=int, default=300)
    monitor.add_argument("--live-max-delay-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    load_environment()
    if args.command == "monitor":
        from smart_money.monitor import run_monitor

        summary = run_monitor(
            args.watchlist,
            args.output,
            watch=args.watch,
            offline_mas=args.offline_mas,
            block_batch_size=args.block_batch_size,
            poll_seconds=args.poll_seconds,
            max_block_age_seconds=args.max_block_age_seconds,
            live_max_delay_seconds=args.live_max_delay_seconds,
        )
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return 1 if summary["health"]["status"] in {"BLOCKED", "RECOVERY_PENDING", "NO_MONITORED_WALLETS"} else 0
    if args.command == "watchlist":
        if args.migrate_storage:
            if args.input or args.offline or args.as_of or args.repair or args.repair_wallet or args.due or args.watch:
                parser.error("Migration requires an isolated output without refresh options")
            print(json.dumps(migrate_watchlist(args.migrate_storage, args.output), ensure_ascii=False))
            return 0
        if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
            parser.error("--poll-seconds must be finite and positive")
        if (args.due or args.watch) and (args.input or args.offline or args.as_of or args.repair or args.repair_wallet):
            parser.error("--due/--watch uses current live facts; omit input, offline, as-of and repair")
        inputs = json.loads(args.input.read_text(encoding="utf-8")) if args.input else None
        while True:
            try:
                watchlist_result = refresh_watchlist(
                    args.output,
                    inputs=inputs,
                    offline=args.offline,
                    repair=args.repair,
                    repair_wallet=args.repair_wallet,
                    as_of=args.as_of,
                    categories=args.categories,
                    due=args.due or args.watch,
                )
                summary = dict(watchlist_result["summary"])
                if args.due or args.watch:
                    maintenance = watchlist_result["maintenance"]
                    summary["maintenance"] = {
                        "daily_date": maintenance["daily_date"],
                        "pending_wallets": len(maintenance["pending"]),
                        "discovery_pending": maintenance["discovery_pending"],
                        "settlement_check": maintenance["settlement_check"],
                    }
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                if not args.watch:
                    return int(
                        bool(
                            args.due
                            and (
                                maintenance["pending"]
                                or maintenance["discovery_pending"]
                                or maintenance["settlement_check"]["status"] != "OK"
                            )
                        )
                    )
            except (OSError, ValueError) as exc:
                if not args.watch:
                    raise
                print(json.dumps({"maintenance_error": str(exc)}, ensure_ascii=False), flush=True)
            time.sleep(args.poll_seconds)
    request = AnalysisRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
    engine = MasEngine(llm_enabled=not args.offline, osint_enabled=not args.offline)
    result = engine.run(request.candidate, context=request.context, evidence=request.evidence)
    if args.output is None:
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    else:
        write_json(args.output.resolve(), result.model_dump(mode="json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
