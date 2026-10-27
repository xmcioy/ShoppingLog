#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
购物记录站点数据同步脚本
从 data/shopping.db 生成 site/data.js，供前端 index.html 消费。

用法:
  python3 scripts/sync_site.py

输出:
  site/data.js  ->  window.SHOPPING_DATA = [ ...items... ]
"""

import json
import os
from pathlib import Path

import db
import shop  # 复用低耦合分表路由与扩展表定义

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "shopping.db"
SITE_DIR = ROOT / "site"
DATA_JS = SITE_DIR / "data.js"

JSON_FIELDS = ("images", "nutrition", "flavors")
TEXT_FIELDS = ("tags",)


def row_to_item(row: dict):
    item = dict(row)
    for f in JSON_FIELDS:
        v = item.get(f)
        if v:
            try:
                item[f] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                item[f] = None if f == "nutrition" else []
        else:
            item[f] = [] if f == "images" else None
    for f in TEXT_FIELDS:
        v = item.get(f)
        item[f] = [t.strip() for t in (v or "").split(",") if t.strip()]
    # 缺省字段兜底
    item.setdefault("currency", "CNY")
    item.setdefault("quantity", 1)
    return item


def main():
    con = db.connect()
    joins, ext_cols = shop.ext_join_sql()
    rows = con.execute(
        f"SELECT items.*, {ext_cols} FROM items {joins} "
        "ORDER BY purchase_date DESC, id DESC"
    ).fetchall()
    con.close()

    items = [row_to_item(shop.merged_dict(r)) for r in rows]
    payload = "// 本文件由 scripts/sync_site.py 自动生成，请勿手工编辑\n"
    payload += "window.SHOPPING_DATA = " + json.dumps(items, ensure_ascii=False, indent=1) + ";\n"

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_JS.write_text(payload, encoding="utf-8")
    print(f"OK: {len(items)} items -> {DATA_JS}")

    # 同步更新 index.html 中 data.js 的版本号，绕过浏览器/Cloudflare 缓存
    # 注意：index.html 是**源码文件**。改写它会产生 git 脏状态，
    # 在多 Agent 并行开发时容易造成互相覆盖。此处仅在 index.html
    # 未被 git 追踪时自动改写；已被追踪则跳过并提示手动处理。
    import re
    import time
    import subprocess
    index_html = SITE_DIR / "index.html"
    if not index_html.exists():
        return
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", "site/index.html"],
                           cwd=str(Path(__file__).resolve().parent.parent),
                           capture_output=True, timeout=5)
        ignored = (r.returncode == 0)
    except Exception:
        ignored = False
    if not ignored:
        print("[提示] site/index.html 是受版本管理的源码文件，"
              "已跳过自动改写 data.js 版本号（避免污染 git 工作区）。"
              "如需刷新缓存请手动编辑版本号，或由 .gitignore 忽略该文件。")
        return
    text = index_html.read_text(encoding="utf-8")
    ts = int(time.time())
    new_text = re.sub(r"data\.js\?v=\d+", f"data.js?v={ts}", text)
    if new_text == text:
        new_text = re.sub(r'(<script src=")data\.js(")', rf"\1data.js?v={ts}\2", text)
    if new_text != text:
        index_html.write_text(new_text, encoding="utf-8")
        print(f"OK: index.html data.js version -> {ts}")


if __name__ == "__main__":
    main()
