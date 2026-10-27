#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShoppingLog 数据库初始化 / 迁移脚本（幂等）

职责:
  1. 建表：**直接复用 server.ensure_schema()**，不再自己维护一份 DDL
     （历史上这里和 schema.sql 各写一套，导致两条建库路径产出不同结构）
  2. items 表的旧数据（user_id 为空）归到第一个管理员
  3. 首次运行创建默认管理员（--admin-password / ADMIN_PASSWORD / 随机）
  4. 初始化系统级设置项（allow_register 默认关闭）

用法:
  python3 scripts/migrate.py [--admin-password xxx]
  ADMIN_PASSWORD=xxx python3 scripts/migrate.py

运行后输出: 默认管理员用户名 / 密码 / token
"""

import argparse
import os
from pathlib import Path

import db
import server  # ensure_schema 是表结构与首次部署引导的唯一权威实现

# 运行目录（数据库、配置等实际数据所在处）；未设置时回退到源码目录
ROOT = Path(os.environ.get("SHOPLOG_ROOT", Path(__file__).resolve().parent.parent))
# 源码目录：表结构定义文件位置，与运行目录分离
SRC_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "shopping.db"
SCHEMA_SQL = SRC_ROOT / "docs" / "schema.sql"

DEFAULT_ADMIN = "admin"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-password", default=None)
    ap.add_argument("--admin-user", default=None)
    args = ap.parse_args()

    # 建库目录必须在连接之前创建：db.connect() 不会自动 mkdir
    ROOT.joinpath("data").mkdir(parents=True, exist_ok=True)

    # 把命令行参数转成 ensure_schema 认识的环境变量
    if args.admin_password:
        os.environ["SHOPLOG_ADMIN_PASSWORD"] = args.admin_password
    if args.admin_user:
        os.environ["SHOPLOG_ADMIN_USER"] = args.admin_user
    elif os.environ.get("ADMIN_PASSWORD") and not os.environ.get("SHOPLOG_ADMIN_PASSWORD"):
        os.environ["SHOPLOG_ADMIN_PASSWORD"] = os.environ["ADMIN_PASSWORD"]

    # 建表 + 补列 + 首次部署引导（全幂等）
    created = server.ensure_schema()
    if created:
        name, password = created
        print(f"[首次初始化] 默认管理员: {name}")
        print(f"[首次初始化] 密码: {password}  ← 请登录后立即修改")
    else:
        print("[初始化] 已存在用户，跳过默认管理员创建")

    conn = db.connect()
    try:
        # 旧数据归属：优先给未删除的管理员，没有就给任意未删除用户
        row = conn.execute(
            "SELECT id, token, username FROM users WHERE is_admin=1 AND deleted_at IS NULL "
            "ORDER BY id LIMIT 1").fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, token, username FROM users WHERE deleted_at IS NULL "
                "ORDER BY id LIMIT 1").fetchone()
        if row is None:
            print("[初始化] 没有可用用户，跳过数据归属")
            return
        admin_id, token, admin_name = row
        n = conn.execute("UPDATE items SET user_id=? WHERE user_id IS NULL",
                         (admin_id,)).fetchone()
        # 默认设置（清理旧模型配置键，仅保留非模型配置项）
        conn.execute("INSERT OR IGNORE INTO settings (skey, value) VALUES ('allow_register', '0')")
        conn.execute("DELETE FROM settings WHERE skey IN "
                     "('model_base_url','model_api_key','model_name')")
        conn.commit()
    finally:
        conn.close()

    print(f"[初始化] 管理员账号: {admin_name}")
    print(f"[初始化] 用户 Token: {token}  ← 供 skill/CLI 使用，请妥善保存")
    print("[初始化] 完成")


if __name__ == "__main__":
    main()
