#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重算健康评分：把「营养 + 配料」按后端规则算出分，写回 items。

用途：
  - 历史数据补齐：0.5.4 之前 /api/add 不算分，老记录的 health_score 全是 NULL
  - 评分规则改动后全量刷新，保证库里的分与当前规则一致

用法（操作的是运行目录 $SHOPLOG_ROOT 的库，不是源码目录）：
  python3 scripts/recalc_health.py              # 只补算「还没评分」的记录（默认）
  python3 scripts/recalc_health.py --all        # 全量重算（会覆盖人工改过的分）
  python3 scripts/recalc_health.py --dry-run    # 只看会改成什么，不落库

评分唯一权威是 scripts/health_score.py，本脚本不另写规则。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db            # noqa: E402
import health_score  # noqa: E402
import shop          # noqa: E402


def parse_nutrition(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    try:
        import json
        v = json.loads(raw)
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全量重算（默认只补算未评分的）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = ap.parse_args()

    root = os.environ.get("SHOPLOG_ROOT") or str(Path(__file__).resolve().parent.parent)
    print(f"[recalc] 运行目录: {root}")

    conn = db.connect()
    joins, ext_cols = shop.ext_join_sql()
    rows = conn.execute(
        f"SELECT items.id, items.name, items.category, items.health_score, "
        f"{ext_cols} FROM items {joins} ORDER BY items.id").fetchall()

    changed = skipped = cleared = 0
    for r in rows:
        d = shop.merged_dict(r)   # 扩展字段带表前缀别名，必须先拍平
        cur = d.get("health_score")
        if cur is not None and not args.all:
            skipped += 1
            continue
        nut = parse_nutrition(d.get("nutrition"))
        ing = d.get("ingredients")
        if not nut and not (ing and str(ing).strip()):
            # 没有营养也没有配料（多半是非食品）→ 不给分，全量模式下清掉旧分
            if args.all and cur is not None:
                cleared += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE items SET health_score=NULL, health_level=NULL, "
                        "health_reason=NULL WHERE id=?", (d["id"],))
            else:
                skipped += 1
            continue
        hs = health_score.score_food(nut, ing)
        changed += 1
        tag = "DRY " if args.dry_run else ""
        print(f"{tag}#{d['id']} {str(d.get('name'))[:24]:<24} "
              f"{hs['score']:>3} 分 {hs['grade']} {hs['level']}"
              + (f"  (旧 {cur})" if cur is not None else ""))
        if not args.dry_run:
            conn.execute(
                "UPDATE items SET health_score=?, health_level=?, health_reason=? WHERE id=?",
                (hs["score"], hs["level"], hs["summary"], d["id"]))
    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"[recalc] 完成：重算 {changed} 条，跳过 {skipped} 条，清除 {cleared} 条"
          + ("（dry-run，未写库）" if args.dry_run else ""))


if __name__ == "__main__":
    main()
