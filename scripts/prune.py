"""按保留期清理会话状态（checkpoint）。

    uv run python scripts/prune.py                 # 预演：只看会删什么
    uv run python scripts/prune.py --apply         # 真删
    uv run python scripts/prune.py --days 7 --apply

默认 **不删除**，必须显式 --apply。会话状态一旦删掉就找不回来，而清理通常是
在半夜的定时任务里跑的，误删不会有任何人当场发现。

注意工单库（tickets.sqlite）：里面有等待人工审批的中断状态，删掉等于把审批队列清空。
建议对工单库单独设定更长的保留期，或在确认无待审批后再清理。
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

from agent_platform.core.checkpoint import SqliteCheckpointSaver


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="清理过期会话状态")
    parser.add_argument("--dir", type=Path, default=Path("./data/checkpoints"))
    parser.add_argument("--days", type=float, default=30.0, help="保留天数")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    parser.add_argument("--limit", type=int, default=10, help="每库展示的最近会话数")
    args = parser.parse_args()

    if not args.dir.is_dir():
        print(f"目录不存在: {args.dir}")
        return 1

    cutoff = time.time() - args.days * 86400
    total = 0
    failures = 0

    for db_path in sorted(args.dir.glob("*.sqlite")):
        saver = SqliteCheckpointSaver(db_path)
        try:
            stats = saver.thread_stats(limit=args.limit)
            stale = saver.prune_threads(older_than=cutoff, dry_run=not args.apply)
            print(f"\n{db_path}  会话 {len(saver.thread_stats(limit=10000))} 个，"
                  f"过期待清 {len(stale)} 个（阈值 {args.days:g} 天）")
            for thread_id, ts, count in stats:
                mark = "过期" if ts < cutoff else "保留"
                print(f"  [{mark}] {thread_id[:44]:44} {_fmt(ts)}  checkpoint={count}")
            if args.apply:
                total += len(stale)
        except Exception as exc:
            failures += 1
            print(f"  处理失败: {type(exc).__name__}: {exc}")
        finally:
            saver.close()

    if args.apply:
        print(f"\n已删除 {total} 个会话")
    else:
        print("\n预演完成。加 --apply 才会真正删除。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
