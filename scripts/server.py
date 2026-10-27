#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShoppingLog 0.2 多用户 API 服务（本地常驻，仅监听 127.0.0.1，由 nginx 反代）

鉴权（二选一）:
  Cookie: shopping_token=<user.token>
  Authorization: Bearer <user.token>

接口:
  GET  /api/health      健康检查
  POST /api/register    注册 {username,password}（受 allow_register 控制）
  POST /api/login       登录 {username,password} → Set-Cookie + JSON token
  GET  /api/auth-check  鉴权判定（nginx auth_request 用）200/401
  GET  /api/me          当前用户信息
  GET  /api/items       当前用户记录列表
  POST /api/add         结构化新增 {字段...}
  POST /api/update      修改 {id, 字段...}
  POST /api/delete      删除 {id}
  POST /api/upload      多图本地 OCR 上传 {images:[base64...], 字段...} → 本地 OCR + 健康评分后写入
  GET  /api/settings    读取设置（系统级脱敏 + 用户级）
  POST /api/settings    写设置（系统级仅管理员，用户级本人）

说明:
  - 本服务全程本地处理，不调用任何模型 API：OCR 走 macOS Vision framework，
    健康评分为纯规则算法（scripts/local_ocr.py / scripts/health_score.py）。
  - 上传支持一次多张图片；后端逐张本地 OCR，识别结果自动填充字段；
    前端提交的用户字段优先于 OCR 结果，全部字段均可手动编辑。

用法:
  python3 scripts/server.py [--port 8787]
"""

import argparse
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db
import shop  # 复用分表路由 / 图片归档 / 行转换
import local_ocr
import health_score
import mcp_tools
import version as version_info  # 版本号唯一来源：仓库根 VERSION 文件

ROOT = Path(os.environ.get("SHOPLOG_ROOT", Path(__file__).resolve().parent.parent))
# 源码目录（与运行目录 ROOT 分离）：表结构定义文件在源码里，运行目录只有数据
SRC_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = SRC_ROOT / "docs" / "schema.sql"
DB = ROOT / "data" / "shopping.db"
COOKIE_NAME = "shopping_token"
COOKIE_MAX_AGE = 15552000  # 180 天
TOKEN_BYTES = 32

# 公开广场允许暴露的字段白名单。
# 原则：**购买记录不公开**（价格、购买日期、渠道、地点、数量等），
# 其他与「商品/评价」有关的信息可以共享。
# 采用白名单而非黑名单：新增字段默认不公开，避免以后加字段时不小心泄露。
#
# 0.5.2 起公开广场改成「小红书式大图卡片」，因此额外放出：
#   - images：商品图（大图展示的主体；用户公开分享即视为同意展示图片）
#   - ingredients / nutrition / nutrition_analysis / eating_method：成分表、营养、食用方式
#   - 厂商、产地、规格、贮存等「其他信息」
# 仍**不放**的日期类字段：produced_date / expiry_date（生产日期、到期日属于购买记录，
# 只在本人记录页的「购买记录」折叠块里出现）。
PUBLIC_FIELDS = (
    "id", "user_id", "name", "brand", "category", "subcategory",
    "rating", "review", "share_note", "repurchase", "tags",
    "health_score", "health_level", "health_reason", "health_grade",
    "product_group", "flavor_name", "owner_name",
    # 图文与成分信息（公开广场大图卡片 + 默认折叠的详情）
    "images", "ingredients", "nutrition", "nutrition_analysis",
    "eating_method", "manufacturer", "manufacturer_address",
    "origin", "specs", "tips", "allergens", "storage_condition",
    "standard_no", "shelf_life",
)


# --------------------------------------------------------------------------
# 数据库 / 认证基础
# --------------------------------------------------------------------------
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
PASSWORD_DIGITS = "23456789"
# 自动生成的密码会被用户从控制台复制，故意避开 shell 里有特殊含义的 $ & ` " ' \ 等，
# 免得粘进命令行时被展开。用户自己设置的密码不受此限制。
PASSWORD_SYMBOLS = "!#%*-_=+"


def gen_password(length: int = 16) -> str:
    """生成满足 check_password_strength 的随机密码（大小写+数字+符号齐全）。"""
    pool = PASSWORD_ALPHABET + PASSWORD_DIGITS + PASSWORD_SYMBOLS
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(max(12, length)))
        if check_password_strength(pw) is None:
            return pw


def bootstrap_admin(conn):
    """首次部署引导：users 表为空时创建默认管理员。

    没有这一步，全新部署会陷入死锁：注册默认关闭（allow_register=0），
    库里又没有账号，谁都登录不进来，也没有管理员能去打开注册开关。

    密码来源优先级：SHOPLOG_ADMIN_PASSWORD 环境变量 > 随机生成。
    随机密码会打印到控制台并写入 data/INITIAL_ADMIN.txt（权限 0600）。

    返回 (username, password) 或 None（已有用户，跳过）。
    """
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if n:
        return None
    username = (os.environ.get("SHOPLOG_ADMIN_USER") or "admin").strip()[:32] or "admin"
    password = os.environ.get("SHOPLOG_ADMIN_PASSWORD") or gen_password()
    token = secrets.token_hex(TOKEN_BYTES)
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, token, is_admin, "
        "is_active, created_at) VALUES (?,?,?,?,1,1,datetime('now','localtime'))",
        (username, hash_password(password), username, token),
    )
    conn.commit()
    # 落盘一份，方便从部署脚本 / 容器日志里找不到时回查
    try:
        notice = ROOT / "data" / "INITIAL_ADMIN.txt"
        notice.parent.mkdir(parents=True, exist_ok=True)
        notice.write_text(
            "ShoppingLog 首次部署生成的默认管理员\n"
            "请登录后立即修改密码，并删除本文件。\n\n"
            f"用户名: {username}\n密码:   {password}\n",
            encoding="utf-8",
        )
        os.chmod(notice, 0o600)
    except OSError:
        pass
    return username, password


def _add_missing_columns(conn, table, cols):
    """给已存在的旧表补列。表不存在时什么都不做（建表交给 schema.sql）。"""
    try:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return
    if not existing:          # 表不存在
        return
    for name, ddl in cols:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def ensure_schema():
    """建表 + 补列 + 清理旧配置 + 首次部署引导。幂等，可重复执行。

    表结构唯一权威源是源码目录的 docs/schema.sql。运行目录（SHOPLOG_ROOT）
    只有数据，不含 docs/，所以这里不能按运行目录去找 schema.sql —— 那样在
    全新部署时会跳过建表，随后 ALTER TABLE 打在不存在的表上导致启动失败。

    **升级顺序很关键**：schema.sql 里有针对新列的 `CREATE INDEX`
    （`idx_users_deleted`、`idx_items_vis`）。旧库上这些列还不存在，
    直接 executescript 会在建索引那一步报 `Key column doesn't exist`，
    整个迁移中断。所以必须**先补列，再执行 schema.sql**。

    返回值：首次创建管理员时返回 (username, password)，否则 None。
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    # 第一步：旧库先补齐新列（全新库此时表还不存在，函数直接跳过）
    _add_missing_columns(conn, "users", [
        ("is_active", "INTEGER DEFAULT 1"),
        ("deleted_at", "VARCHAR(32)"),
    ])
    _add_missing_columns(conn, "items", [
        ("user_id", "INTEGER"),
        ("health_score", "REAL"),
        ("health_level", "TEXT"),
        ("health_reason", "TEXT"),
        ("visibility", "VARCHAR(16) DEFAULT 'public'"),
        ("share_note", "TEXT"),
    ])
    conn.commit()
    # 第二步：执行权威建表语句（幂等）
    if SCHEMA_SQL.exists():
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    else:
        # 权威文件缺失（例如裁剪部署）：退回 shop.connect() 的建表逻辑
        conn.close()
        shop.connect().close()
        conn = db.connect()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    if "user_id" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN user_id INTEGER")
    if "health_score" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN health_score REAL")
    if "health_level" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN health_level TEXT")
    if "health_reason" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN health_reason TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id)")
    # 清理旧模型配置：settings 键 + users.model_token 列
    conn.execute("DELETE FROM settings WHERE skey IN ('model_base_url','model_api_key','model_name')")
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "model_token" in ucols:
        try:
            conn.execute("ALTER TABLE users DROP COLUMN model_token")
        except Exception:
            pass
    # 增量补列：新版本字段（幂等，旧库自动升级）
    ucols2 = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "is_active" not in ucols2:
        conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1")
    if "deleted_at" not in ucols2:
        # 回收站：非空表示已软删除。数据全部保留，只是无法登录、不进公开广场。
        # 用 VARCHAR 而非 TEXT：MySQL 不允许 TEXT 列直接建索引。
        conn.execute("ALTER TABLE users ADD COLUMN deleted_at VARCHAR(32)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_deleted ON users(deleted_at)")
    icols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    if "visibility" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN visibility VARCHAR(16) DEFAULT 'public'")
    if "share_note" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN share_note TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_vis ON items(visibility)")
    # 购买记录拆表（0.6.0）：一次购买一行。旧库里每条 item 视为「买过一次」，
    # 把购买字段原样回填成首条 purchases，之后每次复购只新增 purchase 行。
    conn.execute("""
        INSERT INTO purchases
            (user_id, item_id, purchase_date, price, currency, quantity, unit_price,
             original_price, paid_price, gift_card_value, jd_bean_value, channel, place, created_at)
        SELECT user_id, id,
               COALESCE(purchase_date, ''), price, currency, quantity, unit_price,
               original_price, paid_price, gift_card_value, jd_bean_value, channel, place, created_at
        FROM items
        WHERE NOT EXISTS (SELECT 1 FROM purchases p WHERE p.item_id = items.id)
    """)
    # 回填可能出现空日期（旧脏数据），统一兜成今天，避免 NOT NULL 约束在 MySQL 上报错
    conn.execute("UPDATE purchases SET purchase_date=? WHERE purchase_date IS NULL OR purchase_date=''",
                 (__import__("datetime").date.today().isoformat(),))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchases_item ON purchases(item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user_date ON purchases(user_id, purchase_date)")
    # 品类扩展表补列：列定义的权威源是 shop.EXT_SCHEMAS（查询投影由它生成），
    # docs/schema.sql 是同一份定义的落盘副本。两处曾经漂移过 —— beauty_items 少了
    # allergens / eating_method / standard_no，而 /api/items 的 LEFT JOIN 投影是按
    # EXT_SCHEMAS 生成的，于是每次拉列表都报 "no such column" 直接 500。
    # 这里按代码定义兜底补列：新库由 schema.sql 建好后无副作用，旧库（含两处定义
    # 不同步的库）启动时自动自愈。
    for _t, _cols in shop.EXT_SCHEMAS.items():
        _add_missing_columns(conn, _t, [(c, "TEXT") for c in _cols])
    conn.commit()
    created = bootstrap_admin(conn)
    conn.close()
    return created


def connect():
    conn = db.connect()
    return conn


# --------------------------------------------------------------------------
# 密码策略
# --------------------------------------------------------------------------
PWD_MIN = 8
PWD_MAX = 128


def check_password_strength(pw: str) -> str | None:
    """密码复杂度校验。返回错误信息；通过则返回 None。

    规则（与前端提示保持一致）：
      - 长度 8-128
      - 至少包含 字母、数字、符号 中的两类
      - 不得为纯重复字符（如 aaaaaaaa / 11111111）
      - 不得包含用户名（由调用方单独校验）
    """
    if not isinstance(pw, str):
        return "密码格式错误"
    if len(pw) < PWD_MIN:
        return f"密码至少 {PWD_MIN} 位"
    if len(pw) > PWD_MAX:
        return f"密码最多 {PWD_MAX} 位"
    kinds = 0
    if re.search(r"[A-Za-z]", pw):
        kinds += 1
    if re.search(r"\d", pw):
        kinds += 1
    if re.search(r"[^A-Za-z0-9]", pw):
        kinds += 1
    if kinds < 2:
        return "密码需包含字母、数字、符号中的至少两类"
    if len(set(pw)) == 1:
        return "密码不能为重复字符"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
    return f"pbkdf2_sha256${salt}${h.hex()}"


def verify_password(password_hash: str, password: str) -> bool:
    try:
        algo, salt, hx = password_hash.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
        return h.hex() == hx
    except Exception:
        return False


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE skey=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (skey, value) VALUES (?,?) "
                 "ON CONFLICT(skey) DO UPDATE SET value=excluded.value", (key, value))


def token_from_request(handler) -> str | None:
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    cookies = handler.headers.get("Cookie", "")
    m = re.search(rf"{COOKIE_NAME}=([^;]+)", cookies)
    return m.group(1) if m else None


def get_user(handler):
    token = token_from_request(handler)
    if not token:
        return None
    conn = connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    finally:
        conn.close()
    if not u:
        return None
    d = dict(u)
    # 被管理员停用（is_active=0）或已进回收站（deleted_at 非空）：token 立即失效
    if not d.get("is_active", 1):
        return None
    if d.get("deleted_at"):
        return None
    return d


def require_user(fn):
    def wrapper(handler, *args, **kwargs):
        user = get_user(handler)
        if not user:
            return handler._send(401, {"ok": False, "error": "未登录或 Token 无效"})
        return fn(handler, user, *args, **kwargs)
    return wrapper


def require_admin(fn):
    """管理员专用接口，与 require_user 组合使用。

    用法（注意顺序：require_user 在外层，先注入 user）：
        @require_user
        @require_admin
        def handler(self, user): ...

    require_user 调用时传入 (handler, user)，require_admin 收到 user 后校验再转发。
    """
    def wrapper(handler, user, *args, **kwargs):
        if not user.get("is_admin"):
            return handler._send(403, {"ok": False, "error": "需要管理员权限"})
        return fn(handler, user, *args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# 记录读写（复用 shop.py 的分表逻辑）
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 购买记录（purchases）：一次购买一行，商品档案 items 1:N
# --------------------------------------------------------------------------
# items 上的 price / purchase_date / quantity / channel / place / 各类价格列保留为
# 「最近一次购买」的冗余缓存（写入时与 purchases 同步），历史一律以 purchases 为准。
PURCHASE_COLS = (
    "purchase_date", "price", "currency", "quantity", "unit_price",
    "original_price", "paid_price", "gift_card_value", "jd_bean_value",
    "channel", "place", "note",
)


def _norm_date(v):
    """把日期规范成 YYYY-MM-DD；不合法则返回今天。"""
    s = str(v or "").strip()[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return __import__("datetime").date.today().isoformat()


def build_purchase_sql(user_id, item_id, fields):
    """构造 INSERT purchases 语句。返回 (sql, values)"""
    now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    vals = {"user_id": user_id, "item_id": item_id, "created_at": fields.get("created_at") or now}
    for c in PURCHASE_COLS:
        vals[c] = fields.get(c)
    if vals.get("purchase_date"):
        vals["purchase_date"] = _norm_date(vals["purchase_date"])
    else:
        vals["purchase_date"] = __import__("datetime").date.today().isoformat()
    if vals.get("quantity") in (None, ""):
        vals["quantity"] = 1
    cols = ["user_id", "item_id"] + list(PURCHASE_COLS) + ["created_at"]
    sql = f"INSERT INTO purchases ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    return sql, [vals[c] for c in cols]


def fetch_purchases(conn, item_ids):
    """批量取购买记录。返回 {item_id: [purchase_dict, ...]}（按日期倒序）。"""
    out = {i: [] for i in item_ids}
    if not item_ids:
        return out
    q = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT * FROM purchases WHERE item_id IN ({q}) "
        "ORDER BY purchase_date DESC, id DESC", list(item_ids)).fetchall()
    for r in rows:
        # 不用 shop.row_to_dict：那是给 items 用的（会补 images/tags/nutrition 等）
        d = {k: r[k] for k in r.keys()}
        out.setdefault(d["item_id"], []).append(d)
    return out


def purchase_summary(items, pmap):
    """把购买历史挂到商品上：次数、总花费、均价、首次/最近购买日期。"""
    for it in items:
        ps = pmap.get(it.get("id")) or []
        prices = [p.get("price") for p in ps if p.get("price") is not None]
        dates = sorted(p.get("purchase_date") for p in ps if p.get("purchase_date"))
        it["purchases"] = ps
        it["purchase_count"] = len(ps)
        it["total_spent"] = round(sum(prices), 2) if prices else None
        it["avg_price"] = round(sum(prices) / len(prices), 2) if prices else None
        it["min_price"] = min(prices) if prices else None
        it["max_price"] = max(prices) if prices else None
        it["first_purchase_date"] = dates[0] if dates else None
        it["last_purchase_date"] = dates[-1] if dates else None
    return items


def build_add_sql(user_id, fields):
    """构造 INSERT items 语句（含 user_id）。返回 (sql, values)"""
    cols = ["user_id", "purchase_date", "category", "subcategory", "name", "brand", "price",
            "currency", "quantity", "channel", "place", "rating", "original_price",
            "paid_price", "gift_card_value", "jd_bean_value", "unit_price",
            "review", "tags", "repurchase", "images", "source",
            "purchase_history", "product_group", "flavor_name", "created_at",
            "ingredients", "nutrition", "health_score", "health_level", "health_reason",
            "visibility", "share_note"]
    vals = []
    for c in cols:
        v = fields.get(c)
        if c == "user_id":
            v = user_id
        elif c == "visibility":
            # 默认公开分享；只接受 public / private
            v = "private" if str(v or "").lower() == "private" else "public"
        elif c == "created_at":
            v = v or __import__("datetime").datetime.now().isoformat(timespec="seconds")
        elif c == "purchase_date" and not v:
            v = __import__("datetime").date.today().isoformat()
        vals.append(v)
    sql = f"INSERT INTO items ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    return sql, vals


def ext_insert(conn, item_id, category, fields):
    """新建扩展表行。只写入请求体中出现的字段，其余留空。

    即使没有任何字段值也要建行——保证每条属于该品类的记录在扩展表里
    都有对应行，LEFT JOIN 的结果才稳定。
    """
    ext = shop.CAT_TO_EXT.get(category)
    if not ext:
        return
    ev = {}
    for c in shop.EXT_SCHEMAS[ext]:
        if c == "flavors":
            v = fields.get("flavors")
            if v is not None:
                ev[c] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
        else:
            v = fields.get(c)
            if v is not None:
                ev[c] = v
    exists = conn.execute(f"SELECT 1 FROM {ext} WHERE item_id=?", (item_id,)).fetchone()
    if exists:
        # 已有行 → 增量更新（只写请求体里出现的字段）
        if ev:
            sets = ", ".join(f"{c} = ?" for c in ev)
            conn.execute(f"UPDATE {ext} SET {sets} WHERE item_id=?", [*ev.values(), item_id])
        return
    if ev:
        conn.execute(
            f"INSERT INTO {ext} (item_id, {','.join(ev.keys())}) VALUES (?, {','.join('?' * len(ev))})",
            [item_id, *ev.values()],
        )
    else:
        conn.execute(f"INSERT INTO {ext} (item_id) VALUES (?)", [item_id])


def ext_update(conn, item_id, category, fields):
    """增量更新扩展表：只写入请求体中**实际出现**的字段。

    旧实现是"先 DELETE 扩展行，再按请求体重建"，而网页表单只提交少数字段，
    导致 manufacturer / license / nutrition 等未提交字段被静默清空。
    这里改为：请求体里出现才写，不出现就保留原值。
    """
    ext = shop.CAT_TO_EXT.get(category)
    if not ext:
        return
    has_row = conn.execute(
        f"SELECT 1 FROM {ext} WHERE item_id=?", (item_id,)).fetchone()
    ev = {}
    for c in shop.EXT_SCHEMAS[ext]:
        if c not in fields:
            continue  # 未提交 → 保留原值
        v = fields[c]
        if v is None:
            continue
        if c == "flavors":
            v = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
        ev[c] = v
    if not ev:
        return
    if has_row:
        sets = ", ".join(f"{c} = ?" for c in ev)
        conn.execute(f"UPDATE {ext} SET {sets} WHERE item_id=?",
                     [*ev.values(), item_id])
    else:
        conn.execute(
            f"INSERT INTO {ext} (item_id, {','.join(ev.keys())}) VALUES (?, {','.join('?' * len(ev))})",
            [item_id, *ev.values()],
        )


def row_to_item(row):
    d = shop.row_to_dict(row)
    return d


def parse_nutrition(raw):
    """营养字段可能是 dict（JSON body）也可能是 JSON 字符串（DB / 表单），统一成 dict。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return v if isinstance(v, dict) else None
    return None


def jsonify_fields(body):
    """把 API 里可能以**对象形式**提交的字段序列化成 JSON 文本。

    网页表单和 MCP 工具都会先序列化，但直接调 /api/add、/api/update 的调用方
    （脚本、测试）可能直接传 dict/list —— 以前会一路带到 SQL 里，
    以「RemoteDisconnected」的形式把连接崩掉。这里统一兜住。
    """
    for f in ("nutrition", "flavors", "purchase_history", "images"):
        v = body.get(f)
        if isinstance(v, (dict, list)):
            body[f] = json.dumps(v, ensure_ascii=False)
    return body


def compute_health(fields):
    """按「营养 + 配料」算健康评分，返回 hs dict；两者都没有时返回 None（不硬凑分）。

    服务端是健康评分的**唯一权威来源**：新增（/api/add，含 OCR 上传）与
    更新（/api/update 改了营养/配料）都走这里，前端只负责展示，不再另算一套。
    """
    nut = parse_nutrition(fields.get("nutrition"))
    ing = fields.get("ingredients")
    if not nut and not (ing and str(ing).strip()):
        return None
    cat = (fields.get("category") or "").strip()
    if cat == "食品" or cat == "零食":
        return health_score.score_food(nut, ing)
    # 非食品（日化/美妆等）有配料表时按成分规则评分，识别高风险成分并给出提示
    return health_score.score_cosmetic(ing)


def apply_health(fields, hs):
    """把评分结果写回字段字典（供 INSERT / UPDATE 使用）。"""
    if not hs:
        return fields
    fields["health_score"] = hs["score"]
    fields["health_level"] = hs["level"]
    fields["health_reason"] = hs["summary"]
    return fields


def health_grade_sql(letter):
    """健康档次 → SQL 条件片段（公开广场按 A/B/C/D 筛选用）。

    A/B/C/D 走 health_score 区间（阈值来自 health_score.GRADE_BANDS，与前端一致）；
    `none` = 还没评分（没有营养与配料数据的非食品）。未知档位返回 None。
    """
    if not letter:
        return None
    if str(letter).strip().lower() == "none":
        return "items.health_score IS NULL"
    band = health_score.band_of(letter)
    if not band:
        return None
    lo, hi, _name = band
    if hi is None:
        return f"items.health_score >= {int(lo)}"
    return f"items.health_score >= {int(lo)} AND items.health_score < {int(hi)}"


def public_view(it):
    """把一条完整记录裁成「可公开」的字段。

    购买记录（价格 / 购买日期 / 渠道 / 地点 / 数量）一律不输出，
    只保留商品本身的属性、图片、成分与评价。将来给 items 加新字段时，
    默认不会泄露，除非显式加进 PUBLIC_FIELDS。
    """
    out = {k: it.get(k) for k in PUBLIC_FIELDS if k in it}
    out["owner_name"] = it.get("owner_name") or "匿名用户"
    return out


def _prepare_item(row):
    """把数据库行转成前端可用的 item dict：合并扩展字段 + 反序列化 JSON 字段。"""
    it = shop.merged_dict(row)
    for f in ("images", "nutrition", "flavors"):
        v = it.get(f)
        if v:
            try:
                it[f] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                it[f] = [] if f == "images" else None
        else:
            it[f] = [] if f == "images" else None
    tags = it.get("tags") or ""
    it["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    # 健康档次（A/B/C/D）由后端按 health_score 统一换算，前端只展示、不另算
    it["health_grade"] = health_score.grade_of(it.get("health_score"))[0]
    return it


def _abs_image(path):
    """把 items.images 里的相对路径（images/...）转成绝对路径；越界返回 None。

    只接受 images/ 前缀，并强制解析后仍位于 ROOT/images 下，防目录穿越。
    """
    p = (path or "").strip().lstrip("/")
    if not p.startswith("images/"):
        return None
    base = (ROOT / "images").resolve()
    target = (ROOT / p).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def delete_image_files(paths):
    """删除磁盘上的图片文件，返回成功删除数量。路径必须位于 images/ 下。"""
    n = 0
    for p in paths or []:
        t = _abs_image(p)
        if t and t.is_file():
            try:
                t.unlink()
                n += 1
            except OSError:
                pass
    return n


def images_to_json(v):
    """把请求体里的 images 规整成 JSON 字符串。

    接受三种形态：
      - None                       → None（不改动）
      - 已归档路径数组或 JSON 字符串 → 只保留 images/ 前缀的路径
      - 单个路径字符串             → 包成数组
    旧版 base64 直传走 /api/upload，不经过这里。
    """
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return json.dumps([], ensure_ascii=False)
        if s.startswith("["):
            try:
                v = json.loads(s)
            except json.JSONDecodeError:
                return None
        else:
            v = [s]
    if not isinstance(v, list):
        return None
    keep = [str(x).strip() for x in v
            if isinstance(x, str) and str(x).strip().startswith("images/")]
    return json.dumps(keep, ensure_ascii=False)


def _load_images(raw):
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def save_uploaded_images(imgs, purchase_date=None, name=None):
    """把 base64 图片解码、归档到 images/YYYY-MM/，返回相对路径列表。"""
    import datetime as _dt
    pdate = purchase_date or _dt.date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(pdate)):
        pdate = _dt.date.today().isoformat()
    tmp_paths = []
    try:
        for i, raw in enumerate(imgs):
            if not isinstance(raw, str):
                continue
            if "," in raw:
                raw = raw.split(",", 1)[1]
            try:
                img_bytes = base64.b64decode(raw)
            except Exception:
                continue
            if not img_bytes:
                continue
            tmp = Path(tempfile.gettempdir()) / f"shoppinglog_img_{secrets.token_hex(8)}_{i}.img"
            tmp.write_bytes(img_bytes)
            tmp_paths.append(tmp)
        if not tmp_paths:
            return []
        return shop.store_images([str(t) for t in tmp_paths], pdate,
                                 str(name or "未识别商品")[:200])
    finally:
        for t in tmp_paths:
            t.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, location, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def log_message(self, *args):
        pass

    # ---------------- GET ----------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send(200, {"ok": True, "service": "shoppinglog-api", "version": version_info.get_version()})
        elif path == "/api/version":
            # 免鉴权版本号（nginx 里单独放行）：网页右下角徽标用它判断线上跑到哪了
            self._send(200, {"ok": True, "service": "shoppinglog-api", "version": version_info.get_version()})
        elif path == "/api/auth-check":
            user = get_user(self)
            self._send(200, {"ok": True, "user": user["username"]}) if user else self._send(401, {"ok": False})
        elif path == "/api/me":
            self._handle_me()
        elif path == "/api/items":
            self._handle_items()
        elif path == "/api/public":
            self._handle_public_items()
        elif path == "/api/public-image":
            self._handle_public_image()
        elif path == "/api/settings":
            self._handle_settings_get()
        elif path == "/api/users":
            self._handle_users_list()
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _handle_me(self):
        user = get_user(self)
        if not user:
            return self._send(401, {"ok": False, "error": "未登录"})
        self._send(200, {
            "ok": True,
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "is_admin": user["is_admin"],
            "is_active": user.get("is_active", 1) if user.get("is_active") is not None else 1,
            "token_masked": mask_key(user["token"]),
            "created_at": user["created_at"],
        })

    def _handle_items(self):
        user = get_user(self)
        if not user:
            return self._send(401, {"ok": False, "error": "未登录"})
        conn = connect()
        try:
            joins, ext_cols = shop.ext_join_sql()
            rows = conn.execute(
                f"SELECT items.*, {ext_cols} FROM items {joins} WHERE items.user_id=? "
                "ORDER BY purchase_date DESC, id DESC", (user["id"],)
            ).fetchall()
            items = [_prepare_item(r) for r in rows]
            purchase_summary(items, fetch_purchases(conn, [it.get("id") for it in items]))
        finally:
            conn.close()
        self._send(200, {"ok": True, "items": items})

    def _handle_settings_get(self):
        user = get_user(self)
        if not user:
            return self._send(401, {"ok": False, "error": "未登录"})
        conn = connect()
        try:
            sys_ = {
                "allow_register": get_setting(conn, "allow_register", "0"),
            }
        finally:
            conn.close()
        self._send(200, {"ok": True, "system": sys_, "user": {
            "token_masked": mask_key(user["token"]),
            "token": user["token"],
        }})

    # ---- 公开广场：跨用户的公开记录（按商品名分组 + 分页） ----
    def _handle_public_items(self):
        """公开广场：所有 visibility='public' 的记录，按商品名分组后分页返回。

        用途：同一件商品，不同用户可以有各自的记录与评价，互相可见。
        分页对象是**商品分组**而不是原始行 —— 否则同一商品会被切到两页里，
        分组展示就失去意义。
        输出经 public_view() 裁剪：只给商品属性与评价，**不给购买记录**
        （价格/购买日期/渠道/地点/数量/图片），也不给账号敏感信息。
        """
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}

        def _int(name, default, lo, hi):
            try:
                v = int((qs.get(name, [default])[0]))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))

        page = _int("page", 1, 1, 100000)
        size = _int("size", 12, 1, 100)
        keyword = (qs.get("q", [""])[0] or "").strip()
        flat = (qs.get("flat", ["0"])[0] or "0") in ("1", "true", "yes")
        grade = (qs.get("grade", [""])[0] or "").strip()

        where = ["COALESCE(items.visibility,'public')='public'",
                 "COALESCE(users.is_active,1)=1",
                 "users.deleted_at IS NULL"]
        args = []
        if keyword:
            where.append("LOWER(items.name) LIKE ?")
            args.append(f"%{keyword.lower()}%")
        if grade:
            cond = health_grade_sql(grade)
            if not cond:
                return self._send(400, {"ok": False, "error": "grade 只支持 A/B/C/D/none"})
            where.append(cond)
        where_sql = " AND ".join(where)

        conn = connect()
        try:
            joins, ext_cols = shop.ext_join_sql()
            if flat:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM items {joins} LEFT JOIN users ON users.id=items.user_id "
                    f"WHERE {where_sql}", args).fetchone()[0]
                rows = conn.execute(
                    f"SELECT items.*, {ext_cols}, users.display_name AS owner_name "
                    f"FROM items {joins} LEFT JOIN users ON users.id=items.user_id "
                    f"WHERE {where_sql} ORDER BY items.purchase_date DESC, items.id DESC "
                    f"LIMIT ? OFFSET ?", [*args, size, (page - 1) * size]).fetchall()
                items = [public_view(_prepare_item(r)) for r in rows]
                pages = (total + size - 1) // size
                return self._send(200, {
                    "ok": True, "flat": True, "items": items, "groups": [],
                    "total": total, "page": page, "size": size, "pages": pages,
                    "has_prev": page > 1, "has_next": page < pages,
                    "grade": grade or "",
                })

            # 1) 分组统计（分组键 = 小写去空格后的商品名）
            total = conn.execute(
                f"SELECT COUNT(DISTINCT LOWER(TRIM(items.name))) FROM items {joins} "
                f"LEFT JOIN users ON users.id=items.user_id WHERE {where_sql}",
                args).fetchone()[0]
            groups = conn.execute(
                f"SELECT MIN(TRIM(items.name)) AS gname, COUNT(*) AS cnt, "
                f"COUNT(DISTINCT items.user_id) AS owners, "
                f"MAX(items.purchase_date) AS last_date, MIN(items.id) AS first_id "
                f"FROM items {joins} LEFT JOIN users ON users.id=items.user_id "
                f"WHERE {where_sql} GROUP BY LOWER(TRIM(items.name)) "
                f"ORDER BY last_date DESC, first_id DESC LIMIT ? OFFSET ?",
                [*args, size, (page - 1) * size]).fetchall()
            pages = (total + size - 1) // size
            if not groups:
                return self._send(200, {
                    "ok": True, "flat": False, "items": [], "groups": [],
                    "total": 0, "page": page, "size": size, "pages": 0,
                    "has_prev": False, "has_next": False,
                    "grade": grade or "",
                })

            # 2) 取本页各分组的全部记录（不跨页切分同一商品）
            names = [g["gname"] for g in groups]
            keys = [n.strip().lower() for n in names]
            ph = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT items.*, {ext_cols}, users.display_name AS owner_name "
                f"FROM items {joins} LEFT JOIN users ON users.id=items.user_id "
                f"WHERE {where_sql} AND LOWER(TRIM(items.name)) IN ({ph}) "
                f"ORDER BY items.purchase_date DESC, items.id DESC",
                [*args, *keys]).fetchall()
        finally:
            conn.close()

        bucket: dict[str, list] = {}
        for r in rows:
            it = public_view(_prepare_item(r))
            bucket.setdefault(str(it.get("name") or "").strip().lower(), []).append(it)
        out = []
        for g, key in zip(groups, keys):
            # 注意：分组里不带 last_date —— 「最近一次购买时间」也是购买记录
            out.append({
                "name": g["gname"],
                "count": g["cnt"],
                "owner_count": g["owners"],
                "items": bucket.get(key, []),
            })
        self._send(200, {
            "ok": True, "flat": False, "groups": out,
            "items": [x for grp in out for x in grp["items"]],
            "total": total, "page": page, "size": size, "pages": pages,
            "has_prev": page > 1, "has_next": page < pages,
            "grade": grade or "",
        })

    # ---- 公开广场图片：匿名可访问，但只流出「挂在公开记录上」的图片 ----
    def _handle_public_image(self):
        """GET /api/public-image?p=images/2026-10/xxx.jpg

        为什么需要单独的接口：nginx 对 /images/* 做了登录鉴权（图片属于隐私数据），
        而公开广场是免登录页面，直接 <img src="/images/..."> 会被 302 到登录页，
        图片全裂。这里逐个校验：该图必须出现在某条 visibility='public'、且作者账号
        未被停用/删除的记录里，才把文件流出去；否则 403/404。
        图片数据仍然不出运行目录（_abs_image 已做目录穿越防护）。
        """
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        rel = (qs.get("p", [""])[0] or "").strip().lstrip("/")
        target = _abs_image(rel)
        if not target or not target.is_file():
            return self._send(404, {"ok": False, "error": "图片不存在"})

        conn = connect()
        try:
            rows = conn.execute(
                "SELECT items.images AS images FROM items "
                "LEFT JOIN users ON users.id=items.user_id "
                "WHERE items.images LIKE ? AND COALESCE(items.visibility,'public')='public' "
                "AND COALESCE(users.is_active,1)=1 AND users.deleted_at IS NULL",
                (f"%{rel}%",)).fetchall()
        finally:
            conn.close()

        allowed = False
        for r in rows:
            raw = shop.row_to_dict(r).get("images") or ""
            try:
                imgs = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                imgs = []
            if isinstance(imgs, list) and rel in [str(x).strip().lstrip("/") for x in imgs]:
                allowed = True
                break
        if not allowed:
            return self._send(403, {"ok": False, "error": "该图片未公开"})

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            data = target.read_bytes()
        except OSError:
            return self._send(404, {"ok": False, "error": "图片读取失败"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # ---- 管理员：用户列表 ----
    @require_user
    @require_admin
    def _handle_users_list(self, user):
        """默认返回正常用户（不含回收站）；?bin=1 只返回回收站内的用户。"""
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        only_bin = (qs.get("bin", ["0"])[0] or "0") in ("1", "true", "yes")
        conn = connect()
        try:
            cond = "deleted_at IS NOT NULL" if only_bin else "deleted_at IS NULL"
            rows = conn.execute(
                "SELECT id, username, display_name, is_admin, is_active, deleted_at, "
                f"created_at, token FROM users WHERE {cond} ORDER BY id").fetchall()
            n_items = {r[0]: r[1] for r in conn.execute(
                "SELECT user_id, COUNT(*) FROM items GROUP BY user_id").fetchall()}
        finally:
            conn.close()
        users = []
        for r in rows:
            d = dict(r)
            d.pop("token", None)
            d["token_masked"] = mask_key(r["token"])
            d["is_active"] = d.get("is_active") if d.get("is_active") is not None else 1
            d["item_count"] = n_items.get(d["id"], 0)
            users.append(d)
        self._send(200, {"ok": True, "users": users, "bin": only_bin})

    # ---------------- POST ----------------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/register":
            self._handle_register()
        elif path == "/api/login":
            self._handle_login()
        elif path == "/api/logout":
            self._handle_logout()
        elif path == "/api/mcp":
            self._handle_mcp()
        elif path == "/api/add":
            self._handle_add()
        elif path == "/api/update":
            self._handle_update()
        elif path == "/api/delete":
            self._handle_delete()
        elif path == "/api/purchase/delete":
            self._handle_purchase_delete()
        elif path == "/api/upload":
            self._handle_upload()
        elif path == "/api/ocr":
            self._handle_ocr()
        elif path == "/api/images":
            self._handle_images_upload()
        elif path == "/api/images/delete":
            self._handle_image_delete()
        elif path == "/api/settings":
            self._handle_settings_post()
        elif path == "/api/password":
            self._handle_change_password()
        elif path == "/api/token":
            self._handle_reset_token()
        elif path in ("/api/users/active", "/api/users/admin", "/api/users/password",
                      "/api/users/delete", "/api/users/restore", "/api/users/purge"):
            self._handle_user_admin(path.rsplit("/", 1)[-1])
        else:
            self._send(404, {"ok": False, "error": "not found"})

    # ---- 注册 ----
    def _handle_register(self):
        body = self._read_json()
        if not body or not body.get("username") or not body.get("password"):
            return self._send(400, {"ok": False, "error": "缺少 username/password"})
        username = str(body["username"]).strip()
        password = str(body["password"])
        if len(username) < 2 or len(username) > 32:
            return self._send(400, {"ok": False, "error": "用户名长度需 2-32"})
        if not re.match(r"^[A-Za-z0-9_\u4e00-\u9fa5.-]+$", username):
            return self._send(400, {"ok": False, "error": "用户名只能含字母、数字、下划线、中文、点、连字符"})
        err = check_password_strength(password)
        if err:
            return self._send(400, {"ok": False, "error": err})
        if password.lower() == username.lower():
            return self._send(400, {"ok": False, "error": "密码不能与用户名相同"})
        conn = connect()
        try:
            if get_setting(conn, "allow_register", "0") != "1":
                return self._send(403, {"ok": False, "error": "注册未开放，请联系管理员"})
            if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                return self._send(409, {"ok": False, "error": "用户名已存在"})
            token = secrets.token_hex(TOKEN_BYTES)
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, display_name, token, is_admin, created_at) "
                "VALUES (?,?,?,?,0,datetime('now','localtime'))",
                (username, hash_password(password), username, token, ),
            )
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True, "username": username, "token": token})

    # ---- 登录 ----
    def _handle_login(self):
        body = self._read_json()
        if not body or not body.get("username") or not body.get("password"):
            return self._send(400, {"ok": False, "error": "缺少 username/password"})
        conn = connect()
        try:
            u = conn.execute("SELECT * FROM users WHERE username=?", (str(body["username"]).strip(),)).fetchone()
        finally:
            conn.close()
        if not u or not verify_password(u["password_hash"], str(body["password"])):
            return self._send(401, {"ok": False, "error": "用户名或密码错误"})
        if u.get("deleted_at"):
            # 回收站账号按"不存在"处理，不泄露它曾经存在
            return self._send(401, {"ok": False, "error": "用户名或密码错误"})
        if not u.get("is_active", 1):
            return self._send(403, {"ok": False, "error": "账号已被停用，请联系管理员"})
        # 0.1: 登录即轮换 token（旧 token 失效）
        new_token = secrets.token_hex(TOKEN_BYTES)
        conn = connect()
        try:
            conn.execute("UPDATE users SET token=? WHERE id=?", (new_token, u["id"]))
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True, "username": u["username"], "token": new_token}, extra_headers={
            "Set-Cookie": f"{COOKIE_NAME}={new_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={COOKIE_MAX_AGE}",
        })

    # ---- 记录增删改 ----
    @require_user
    def _handle_add(self, user):
        """新增。两种模式：

        1) 带 `item_id`：**复购已有商品** —— 只新增一条 purchases，商品档案不动
           （可选同时更新评分/评价/图片等商品字段）。
        2) 不带 `item_id`：新建商品档案 + 首条购买记录。
        """
        body = self._read_json()
        if not body:
            return self._send(400, {"ok": False, "error": "请求体为空"})
        jsonify_fields(body)
        conn = connect()
        try:
            reuse_id = body.get("item_id")
            if reuse_id:
                try:
                    reuse_id = int(reuse_id)
                except (TypeError, ValueError):
                    return self._send(400, {"ok": False, "error": "item_id 无效"})
                row = conn.execute("SELECT * FROM items WHERE id=? AND user_id=?",
                                   (reuse_id, user["id"])).fetchone()
                if not row:
                    return self._send(403, {"ok": False, "error": "商品不存在或无权限"})
                # 只在显式提交时更新商品字段（评分/评价/图片等），未提交的原样保留
                upd, vals = [], []
                for f in ("rating", "review", "repurchase", "tags", "visibility", "share_note"):
                    if f in body and body[f] is not None:
                        v = body[f]
                        if f == "visibility":
                            v = "private" if str(v).lower() == "private" else "public"
                        upd.append(f"{f} = ?")
                        vals.append(v)
                if body.get("images") is not None:
                    img_json = images_to_json(body.get("images"))
                    if img_json is not None:
                        upd.append("images = ?")
                        vals.append(img_json)
                if upd:
                    conn.execute(f"UPDATE items SET {', '.join(upd)} WHERE id=? AND user_id=?",
                                 [*vals, reuse_id, user["id"]])
                # 购买字段缺省沿用上一次购买（复购时价格/渠道通常不变，省得重复填）
                last = conn.execute(
                    "SELECT * FROM purchases WHERE item_id=? ORDER BY purchase_date DESC, id DESC LIMIT 1",
                    (reuse_id,)).fetchone()
                pf = {}
                if last:
                    for c in PURCHASE_COLS:
                        pf[c] = last[c]
                for k in PURCHASE_COLS:
                    v = body.get(k)
                    if v is not None and v != "":
                        pf[k] = v
                pf["purchase_date"] = (body.get("purchase_date")
                                       or (last["purchase_date"] if last else None)
                                       or __import__("datetime").date.today().isoformat())
                sql, pvals = build_purchase_sql(user["id"], reuse_id, pf)
                cur = conn.execute(sql, pvals)
                conn.commit()
                return self._send(200, {"ok": True, "id": reuse_id,
                                        "purchase_id": cur.lastrowid, "reused": True})

            if not body.get("name"):
                return self._send(400, {"ok": False, "error": "缺少 name"})
            img_json = images_to_json(body.get("images"))
            if img_json is not None:
                body["images"] = img_json
            # 健康评分：服务端统一算，前端/MCP 不用自己算
            # （显式传了 health_score 的以调用方为准，比如人工复核过的分）
            if body.get("health_score") is None and body.get("health_level") is None:
                apply_health(body, compute_health(body))
            sql, vals = build_add_sql(user["id"], body)
            cur = conn.execute(sql, vals)
            item_id = cur.lastrowid
            ext_insert(conn, item_id, body.get("category"), body)
            psql, pvals = build_purchase_sql(user["id"], item_id, body)
            conn.execute(psql, pvals)
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True, "id": item_id})

    @require_user
    def _handle_update(self, user):
        body = self._read_json()
        if not body or not body.get("id"):
            return self._send(400, {"ok": False, "error": "缺少 id"})
        jsonify_fields(body)
        item_id = int(body["id"])
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=? AND user_id=?", (item_id, user["id"])).fetchone()
            if not row:
                return self._send(403, {"ok": False, "error": "记录不存在或无权限"})
            # 改了营养或配料 → 服务端重算健康评分，保持「后端唯一权威」。
            # 没填的那一项取库里的旧值，避免只改配料时把营养判空。
            # 显式传 health_score / health_level 时以调用方为准（人工复核过的分）。
            if body.get("health_score") is None and body.get("health_level") is None \
                    and ("nutrition" in body or "ingredients" in body):
                merged = {k: row[k] for k in row.keys()}
                ext = shop.CAT_TO_EXT.get(body.get("category") or row.get("category") or "其他")
                if ext:
                    ex = conn.execute(f"SELECT * FROM {ext} WHERE item_id=?", (item_id,)).fetchone()
                    if ex:
                        merged.update({k: ex[k] for k in ex.keys()})
                merged.update({k: v for k, v in body.items() if v is not None})
                apply_health(body, compute_health(merged))
            upd, vals = [], []
            allowed = ["purchase_date", "category", "subcategory", "name", "brand", "price",
                       "currency", "quantity", "channel", "place", "rating", "original_price",
                       "paid_price", "gift_card_value", "jd_bean_value", "unit_price",
                       "review", "tags", "repurchase", "source", "purchase_history",
                       "product_group", "flavor_name", "expiry_date", "shelf_life",
                       "ingredients", "nutrition", "health_score", "health_level", "health_reason",
                       "visibility", "share_note"]
            for f in allowed:
                if f in body and body[f] is not None:
                    v = body[f]
                    if f == "visibility":
                        v = "private" if str(v).lower() == "private" else "public"
                    upd.append(f"{f} = ?")
                    vals.append(v)
            if upd:
                conn.execute(f"UPDATE items SET {', '.join(upd)} WHERE id=? AND user_id=?",
                             [*vals, item_id, user["id"]])
            # 图片：body 传的是**最终保留的路径数组**，被移除的磁盘文件同步清理
            removed_files = []
            if body.get("images") is not None:
                new_json = images_to_json(body.get("images"))
                if new_json is not None:
                    old_list = _load_images(row["images"])
                    new_list = _load_images(new_json)
                    removed_files = [p for p in old_list if p not in new_list]
                    conn.execute("UPDATE items SET images=? WHERE id=? AND user_id=?",
                                 (new_json, item_id, user["id"]))
            # 购买记录：编辑时同步「最近一次购买」（或 body.purchase_id 指定的那次）。
            # 商品档案（名称/品牌/成分/评价）改的是 items，购买事实改的是 purchases。
            p_fields = {k: body[k] for k in PURCHASE_COLS
                        if k in body and body[k] is not None and body[k] != ""}
            if p_fields:
                pid = body.get("purchase_id")
                prow = None
                if pid:
                    prow = conn.execute(
                        "SELECT id FROM purchases WHERE id=? AND item_id=? AND user_id=?",
                        (int(pid), item_id, user["id"])).fetchone()
                if prow is None:
                    prow = conn.execute(
                        "SELECT id FROM purchases WHERE item_id=? AND user_id=? "
                        "ORDER BY purchase_date DESC, id DESC LIMIT 1",
                        (item_id, user["id"])).fetchone()
                if "purchase_date" in p_fields:
                    p_fields["purchase_date"] = _norm_date(p_fields["purchase_date"])
                if prow:
                    sets = ", ".join(f"{c} = ?" for c in p_fields)
                    conn.execute(f"UPDATE purchases SET {sets} WHERE id=?",
                                 [*p_fields.values(), prow["id"]])
                else:
                    psql, pvals = build_purchase_sql(user["id"], item_id, p_fields)
                    conn.execute(psql, pvals)
            # 扩展表：
            # - 品类未变 → 增量更新，请求体未提交的字段保留原值
            # - 品类变了 → 迁移：删旧扩展行，在新扩展表按提交字段建行
            new_cat = body.get("category")
            if new_cat is not None:
                old_cat = row["category"] or "其他"
                old_ext = shop.CAT_TO_EXT.get(old_cat)
                target_ext = shop.CAT_TO_EXT.get(new_cat)
                if old_ext and old_ext != target_ext:
                    # 品类迁移：旧扩展行整体丢弃
                    conn.execute(f"DELETE FROM {old_ext} WHERE item_id=?", (item_id,))
                    ext_insert(conn, item_id, new_cat, body)
                else:
                    ext_update(conn, item_id, new_cat, body)
            conn.commit()
        finally:
            conn.close()
        if removed_files:
            delete_image_files(removed_files)
        self._send(200, {"ok": True, "updated": item_id,
                         "images_removed": len(removed_files)})

    @require_user
    def _handle_delete(self, user):
        body = self._read_json()
        if not body or not body.get("id"):
            return self._send(400, {"ok": False, "error": "缺少 id"})
        item_id = int(body["id"])
        conn = connect()
        try:
            row = conn.execute("SELECT id, images FROM items WHERE id=? AND user_id=?",
                               (item_id, user["id"])).fetchone()
            if not row:
                return self._send(403, {"ok": False, "error": "记录不存在或无权限"})
            doomed = _load_images(row["images"])
            conn.execute("DELETE FROM purchases WHERE item_id=? AND user_id=?", (item_id, user["id"]))
            for t in shop.EXT_SCHEMAS:
                conn.execute(f"DELETE FROM {t} WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            conn.commit()
        finally:
            conn.close()
        delete_image_files(doomed)
        self._send(200, {"ok": True, "deleted": item_id})

    # ---- 删除单条购买记录（误记的这一次） ----
    @require_user
    def _handle_purchase_delete(self, user):
        """删掉某一次的购买记录，商品档案保留。

        删完后若还有剩余购买，items 上的「最近一次购买」缓存同步刷新为最新那条；
        若一条不剩，则把缓存留在原样（商品本身仍存在，只是暂时没有购买记录）。
        """
        body = self._read_json() or {}
        pid = body.get("id")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return self._send(400, {"ok": False, "error": "缺少 id"})
        item_id, last = None, None
        conn = connect()
        try:
            row = conn.execute(
                "SELECT p.id, p.item_id FROM purchases p "
                "JOIN items i ON i.id = p.item_id "
                "WHERE p.id=? AND p.user_id=? AND i.user_id=?",
                (pid, user["id"], user["id"])).fetchone()
            if not row:
                return self._send(403, {"ok": False, "error": "购买记录不存在或无权限"})
            item_id = row["item_id"]
            conn.execute("DELETE FROM purchases WHERE id=?", (pid,))
            last = conn.execute(
                "SELECT * FROM purchases WHERE item_id=? ORDER BY purchase_date DESC, id DESC LIMIT 1",
                (item_id,)).fetchone()
            if last:
                sets = ["purchase_date = ?", "price = ?", "currency = ?", "quantity = ?",
                        "unit_price = ?", "original_price = ?", "paid_price = ?",
                        "gift_card_value = ?", "jd_bean_value = ?", "channel = ?", "place = ?"]
                vals = [last["purchase_date"], last["price"], last["currency"], last["quantity"],
                        last["unit_price"], last["original_price"], last["paid_price"],
                        last["gift_card_value"], last["jd_bean_value"], last["channel"], last["place"]]
                conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", [*vals, item_id])
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True, "deleted": pid, "item_id": item_id,
                         "purchase_count": (last is not None)})

    # ---- 本地 OCR 预览（不保存、不调用模型 API） ----
    @require_user
    def _handle_ocr(self, user):
        """OCR 预览。images=base64 数组，或 paths=已归档的相对路径数组。"""
        body = self._read_json() or {}
        imgs = body.get("images") or []
        paths = body.get("paths") or []
        if not imgs and not paths:
            return self._send(400, {"ok": False, "error": "缺少 images 或 paths"})
        if len(imgs) + len(paths) > 20:
            return self._send(400, {"ok": False, "error": "单次最多 20 张图片"})
        targets, temp_paths = [], []
        try:
            # 已归档图片：直接用磁盘上的文件，无需重新传输
            for p in paths:
                t = _abs_image(p)
                if t and t.is_file():
                    targets.append(t)
            for i, raw in enumerate(imgs):
                if not isinstance(raw, str):
                    continue
                if "," in raw:
                    raw = raw.split(",", 1)[1]
                try:
                    img_bytes = base64.b64decode(raw)
                except Exception:
                    continue
                if not img_bytes:
                    continue
                tmp = Path(tempfile.gettempdir()) / f"shoppinglog_ocr_{secrets.token_hex(8)}_{i}.img"
                tmp.write_bytes(img_bytes)
                temp_paths.append(tmp)
                targets.append(tmp)
            if not targets:
                return self._send(400, {"ok": False, "error": "没有可识别的图片"})
            ocr = local_ocr.ocr_fields([str(t) for t in targets])
            hs = health_score.score_food(
                ocr.get("nutrition"), ocr.get("ingredients"),
                ocr.get("additive_count"), ocr.get("additives"),
            )
            fields = {k: v for k, v in ocr.items() if k not in ("ocr_text", "additive_count", "additives")}
            self._send(200, {"ok": True, "fields": fields, "health": hs})
        finally:
            for t in temp_paths:
                t.unlink(missing_ok=True)

    # ---- 多图本地 OCR 上传（不调用任何模型 API） ----
    @require_user
    def _handle_upload(self, user):
        body = self._read_json()
        if not body:
            return self._send(400, {"ok": False, "error": "请求体为空"})
        imgs = body.get("images") or []
        if not imgs and body.get("image_base64"):  # 兼容旧单图格式
            imgs = [body["image_base64"]]
        if not imgs:
            return self._send(400, {"ok": False, "error": "缺少 images（base64 或已归档路径）"})
        if len(imgs) > 20:
            return self._send(400, {"ok": False, "error": "单次最多 20 张图片"})

        # images 里可能混着两种：已归档路径（前端先调 /api/images 上传过）和 base64。
        # 已归档的不要再存一遍，直接用磁盘文件；只有 base64 才落盘。
        pre_paths = []
        b64_items = []
        for raw in imgs:
            if isinstance(raw, str) and raw.strip().lstrip("/").startswith("images/") \
                    and _abs_image(raw) and _abs_image(raw).is_file():
                pre_paths.append(raw.strip().lstrip("/"))
            else:
                b64_items.append(raw)

        tmp_paths = []
        try:
            for i, raw in enumerate(b64_items):
                if not isinstance(raw, str):
                    continue
                if "," in raw:
                    raw = raw.split(",", 1)[1]
                try:
                    img_bytes = base64.b64decode(raw)
                except Exception:
                    continue
                if not img_bytes:
                    continue
                tmp = Path(tempfile.gettempdir()) / f"shoppinglog_upload_{secrets.token_hex(8)}_{i}.img"
                tmp.write_bytes(img_bytes)
                tmp_paths.append(tmp)
            if not tmp_paths and not pre_paths:
                return self._send(400, {"ok": False, "error": "图片解码失败或内容为空"})

            ocr_targets = [str(ROOT / p) for p in pre_paths] + [str(t) for t in tmp_paths]

            # 1) 逐张本地 OCR + 规则解析（零模型 API）
            ocr = local_ocr.ocr_fields(ocr_targets)

            # 2) 本地健康评分（纯规则）
            hs = health_score.score_food(
                ocr.get("nutrition"),
                ocr.get("ingredients"),
                ocr.get("additive_count"),
                ocr.get("additives"),
            )

            # 3) 用户显式提交的字段优先于 OCR 结果（全部字段可手动编辑）
            fields = {}
            editable = ["name", "brand", "category", "subcategory", "price", "currency",
                        "quantity", "channel", "place", "rating", "original_price",
                        "paid_price", "gift_card_value", "jd_bean_value", "unit_price",
                        "review", "tags", "repurchase", "source", "purchase_date",
                        "produced_date", "expiry_date", "shelf_life", "ingredients",
                        "manufacturer", "flavor_name", "product_group",
                        "visibility", "share_note"]
            for k in editable:
                v = body.get(k)
                if v is not None and v != "":
                    fields[k] = v

            # 4) OCR 结果填充未提供字段
            pdate = fields.get("purchase_date") or ocr.get("purchase_date") or __import__("datetime").date.today().isoformat()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", pdate):
                pdate = __import__("datetime").date.today().isoformat()
            name = fields.get("name") or ocr.get("name") or "未识别商品"
            name = str(name)[:200]
            fields.setdefault("purchase_date", pdate)
            fields.setdefault("name", name)
            fields.setdefault("category", ocr.get("category"))
            fields.setdefault("brand", ocr.get("brand"))
            fields.setdefault("price", ocr.get("price"))
            fields.setdefault("quantity", ocr.get("quantity"))
            fields.setdefault("expiry_date", ocr.get("expiry_date"))
            fields.setdefault("shelf_life", ocr.get("shelf_life"))
            fields.setdefault("produced_date", ocr.get("produced_date"))
            fields.setdefault("ingredients", ocr.get("ingredients"))

            # 5) 图片归档（运行目录 images/<日期>/）；已归档的直接沿用
            stored = shop.store_images([str(t) for t in tmp_paths], pdate, name) if tmp_paths else []
            images = pre_paths + stored
            fields["images"] = json.dumps(images, ensure_ascii=False)

            # 6) 营养与健康评分结果随记录保存
            if ocr.get("nutrition"):
                fields["nutrition"] = json.dumps(ocr["nutrition"], ensure_ascii=False)
            fields["health_score"] = hs["score"]
            fields["health_level"] = hs["level"]
            fields["health_reason"] = hs["summary"]

            conn = connect()
            try:
                sql, vals = build_add_sql(user["id"], fields)
                cur = conn.execute(sql, vals)
                ext_insert(conn, cur.lastrowid, fields.get("category"), fields)
                psql, pvals = build_purchase_sql(user["id"], cur.lastrowid, fields)
                conn.execute(psql, pvals)
                conn.commit()
                item_id = cur.lastrowid
            finally:
                conn.close()
            self._send(200, {
                "ok": True, "id": item_id,
                "record": {
                    "name": name, "category": fields.get("category"),
                    "brand": fields.get("brand"), "price": fields.get("price"),
                    "health_score": hs["score"], "health_level": hs["level"],
                },
                "ocr": {
                    "fields": {k: v for k, v in ocr.items() if k != "ocr_text"},
                    "health": hs,
                },
            })
        finally:
            for t in tmp_paths:
                t.unlink(missing_ok=True)

    # ---- 图片预上传（第一步就存下来，不和记录绑定） ----
    @require_user
    def _handle_images_upload(self, user):
        """把图片归档到 images/YYYY-MM/ 并返回路径，不创建记录。

        前端流程：第一步选好图就先传上来 → 用户补完字段保存时，把返回的路径
        作为 images 传给 /api/add 或 /api/update。这样编辑记录也能追加图片，
        而且中途放弃保存只会留下孤儿文件，不会产生空记录。
        """
        body = self._read_json() or {}
        imgs = body.get("images") or []
        if not imgs:
            return self._send(400, {"ok": False, "error": "缺少 images"})
        if len(imgs) > 20:
            return self._send(400, {"ok": False, "error": "单次最多 20 张图片"})
        saved = save_uploaded_images(imgs, body.get("purchase_date"), body.get("name"))
        if not saved:
            return self._send(400, {"ok": False, "error": "图片解码失败或内容为空"})
        self._send(200, {"ok": True, "images": saved})

    # ---- 删除单张图片 ----
    @require_user
    def _handle_image_delete(self, user):
        """删单张图片。带 item_id 时同时从该记录的 images 里摘掉。"""
        body = self._read_json() or {}
        path = str(body.get("path") or "").strip()
        if not path:
            return self._send(400, {"ok": False, "error": "缺少 path"})
        if _abs_image(path) is None:
            return self._send(400, {"ok": False, "error": "路径不合法"})
        item_id = body.get("item_id")
        remaining = None
        if item_id:
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                return self._send(400, {"ok": False, "error": "item_id 不合法"})
            conn = connect()
            try:
                row = conn.execute("SELECT images FROM items WHERE id=? AND user_id=?",
                                   (item_id, user["id"])).fetchone()
                if not row:
                    return self._send(403, {"ok": False, "error": "记录不存在或无权限"})
                cur_imgs = _load_images(row["images"])
                remaining = [p for p in cur_imgs if p != path]
                conn.execute("UPDATE items SET images=? WHERE id=? AND user_id=?",
                             (json.dumps(remaining, ensure_ascii=False), item_id, user["id"]))
                conn.commit()
            finally:
                conn.close()
        removed = delete_image_files([path])
        self._send(200, {"ok": True, "removed": removed, "images": remaining})

    # ---- 设置（仅保留非模型配置项） ----
    def _handle_settings_post(self):
        user = get_user(self)
        if not user:
            return self._send(401, {"ok": False, "error": "未登录"})
        body = self._read_json() or {}
        conn = connect()
        try:
            if user["is_admin"]:
                for key in ("allow_register",):
                    if key in body and body[key] is not None:
                        set_setting(conn, key, str(body[key]).strip())
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True})

    # ---- 退出登录 ----
    def _handle_logout(self):
        """清掉登录 Cookie。

        两个要点：
        1. Cookie 是 HttpOnly 的，**JS 根本删不掉**——必须在服务端下发 Max-Age=0，
           否则点了「退出」页面跳走了，Cookie 还在，一刷新又登录回来了。
        2. 不作废 token。这个 token 同时是 CLI / skill 用的 API Token，
           网页退出不应该把命令行那边一起踢下线。
        """
        self._send(200, {"ok": True}, extra_headers={
            "Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        })

    # ---- MCP 端点（JSON-RPC over HTTP） ----
    def _mcp_api(self, method, path, payload=None, timeout=30):
        """进程内回环调用自己的 /api/*。

        为什么走回环而不是直接查库：MCP 与网页必须共用**同一条**鉴权与校验链路，
        否则很容易出现「网页改了校验、MCP 还是老逻辑」的漂移。多一次本机请求，
        换来的是行为完全一致，这个交易划算。
        """
        port = self.server.server_port
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api{path}",
                                     data=data, method=method)
        # 透传调用方的凭据：Bearer 优先，其次 Cookie
        auth = self.headers.get("Authorization")
        if auth:
            req.add_header("Authorization", auth)
        cookie = self.headers.get("Cookie")
        if cookie:
            req.add_header("Cookie", cookie)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}: {body[:200]}"}

    @require_user
    def _handle_mcp(self, user):
        """MCP JSON-RPC 入口。

        支持单条与批量消息；通知类（无 id）返回 202 且不回 body。
        鉴权与其它接口一致：Bearer 或 Cookie，且必须是启用状态的账号。
        """
        body = self._read_json()
        if body is None:
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "请求体不是合法 JSON"}})
        if isinstance(body, list):
            out = []
            for item in body:
                resp = mcp_tools.handle(item, self._mcp_api)
                if resp is not None:
                    out.append(resp)
            return self._send(200, out)
        resp = mcp_tools.handle(body, self._mcp_api)
        if resp is None:
            return self._send(202, {"ok": True})
        self._send(200, resp)

    # ---- 修改自己的密码 ----
    @require_user
    def _handle_change_password(self, user):
        body = self._read_json() or {}
        old_pw = str(body.get("old_password") or "")
        new_pw = str(body.get("new_password") or "")
        if not old_pw or not new_pw:
            return self._send(400, {"ok": False, "error": "缺少 old_password / new_password"})
        if not verify_password(user["password_hash"], old_pw):
            return self._send(403, {"ok": False, "error": "原密码不正确"})
        err = check_password_strength(new_pw)
        if err:
            return self._send(400, {"ok": False, "error": err})
        if new_pw.lower() == user["username"].lower():
            return self._send(400, {"ok": False, "error": "密码不能与用户名相同"})
        if old_pw == new_pw:
            return self._send(400, {"ok": False, "error": "新密码不能与原密码相同"})
        # 改密后轮换 token：旧会话立即失效（安全默认）
        new_token = secrets.token_hex(TOKEN_BYTES)
        conn = connect()
        try:
            conn.execute("UPDATE users SET password_hash=?, token=? WHERE id=?",
                         (hash_password(new_pw), new_token, user["id"]))
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True, "token": new_token}, extra_headers={
            "Set-Cookie": f"{COOKIE_NAME}={new_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={COOKIE_MAX_AGE}",
        })

    # ---- 重置自己的 API Token ----
    @require_user
    def _handle_reset_token(self, user):
        new_token = secrets.token_hex(TOKEN_BYTES)
        conn = connect()
        try:
            conn.execute("UPDATE users SET token=? WHERE id=?", (new_token, user["id"]))
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True, "token": new_token}, extra_headers={
            "Set-Cookie": f"{COOKIE_NAME}={new_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={COOKIE_MAX_AGE}",
        })

    # ---- 管理员：用户管理（启停 / 设管理员 / 重置密码 / 删除） ----
    @require_user
    @require_admin
    def _handle_user_admin(self, user, action):
        body = self._read_json() or {}
        try:
            target_id = int(body.get("id") or 0)
        except (TypeError, ValueError):
            return self._send(400, {"ok": False, "error": "缺少有效 id"})
        if not target_id:
            return self._send(400, {"ok": False, "error": "缺少 id"})
        conn = connect()
        try:
            t = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
            if not t:
                return self._send(404, {"ok": False, "error": "用户不存在"})
            t = dict(t)
            is_self = (t["id"] == user["id"])

            if action == "active":
                # 不能停用自己，避免把自己锁在外面
                if is_self and not body.get("active"):
                    return self._send(400, {"ok": False, "error": "不能停用自己的账号"})
                active = 1 if body.get("active") else 0
                conn.execute("UPDATE users SET is_active=? WHERE id=?", (active, target_id))
                conn.commit()
                return self._send(200, {"ok": True, "id": target_id, "is_active": active})

            if action == "admin":
                if is_self and not body.get("admin"):
                    return self._send(400, {"ok": False, "error": "不能取消自己的管理员权限"})
                if not body.get("admin"):
                    # 至少保留一个管理员
                    n = conn.execute(
                        "SELECT COUNT(*) FROM users WHERE is_admin=1 AND COALESCE(is_active,1)=1"
                    ).fetchone()[0]
                    if t["is_admin"] and n <= 1:
                        return self._send(400, {"ok": False, "error": "至少保留一名管理员"})
                adm = 1 if body.get("admin") else 0
                conn.execute("UPDATE users SET is_admin=? WHERE id=?", (adm, target_id))
                conn.commit()
                return self._send(200, {"ok": True, "id": target_id, "is_admin": adm})

            if action == "password":
                new_pw = str(body.get("new_password") or "")
                err = check_password_strength(new_pw)
                if err:
                    return self._send(400, {"ok": False, "error": err})
                if new_pw.lower() == t["username"].lower():
                    return self._send(400, {"ok": False, "error": "密码不能与用户名相同"})
                # 重置密码同时换 token，强制该用户重新登录
                conn.execute("UPDATE users SET password_hash=?, token=? WHERE id=?",
                             (hash_password(new_pw), secrets.token_hex(TOKEN_BYTES), target_id))
                conn.commit()
                return self._send(200, {"ok": True, "id": target_id})

            if action == "delete":
                # 一级删除 = 移入回收站（软删除）：保留全部数据，只断开登录
                if is_self:
                    return self._send(400, {"ok": False, "error": "不能删除自己的账号"})
                if t.get("deleted_at"):
                    return self._send(400, {"ok": False, "error": "该用户已在回收站"})
                if t["is_admin"]:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM users WHERE is_admin=1 AND deleted_at IS NULL"
                    ).fetchone()[0]
                    if n <= 1:
                        return self._send(400, {"ok": False, "error": "至少保留一名管理员"})
                conn.execute(
                    "UPDATE users SET deleted_at=datetime('now','localtime'), is_active=0, "
                    "token=? WHERE id=?", (secrets.token_hex(TOKEN_BYTES), target_id))
                conn.commit()
                return self._send(200, {"ok": True, "id": target_id, "deleted": True})

            if action == "restore":
                # 从回收站恢复：回到正常状态（管理员可再单独停用）
                if not t.get("deleted_at"):
                    return self._send(400, {"ok": False, "error": "该用户不在回收站"})
                conn.execute(
                    "UPDATE users SET deleted_at=NULL, is_active=1, token=? WHERE id=?",
                    (secrets.token_hex(TOKEN_BYTES), target_id))
                conn.commit()
                return self._send(200, {"ok": True, "id": target_id, "restored": True})

            if action == "purge":
                # 二级删除 = 从回收站永久删除：**必须先软删**，防止一步误删
                if is_self:
                    return self._send(400, {"ok": False, "error": "不能删除自己的账号"})
                if not t.get("deleted_at"):
                    return self._send(400, {"ok": False,
                                            "error": "请先移入回收站，再从回收站永久删除"})
                if t["is_admin"]:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM users WHERE is_admin=1 AND deleted_at IS NULL"
                    ).fetchone()[0]
                    if n <= 0:
                        return self._send(400, {"ok": False, "error": "至少保留一名管理员"})
                rows = conn.execute("SELECT id, images FROM items WHERE user_id=?",
                                    (target_id,)).fetchall()
                doomed = []
                for r in rows:
                    try:
                        doomed.extend(json.loads(r["images"] or "[]"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                for et in shop.EXT_SCHEMAS:
                    conn.execute(f"DELETE FROM {et} WHERE item_id IN "
                                 f"(SELECT id FROM items WHERE user_id=?)", (target_id,))
                conn.execute("DELETE FROM items WHERE user_id=?", (target_id,))
                conn.execute("DELETE FROM users WHERE id=?", (target_id,))
                conn.commit()
                removed = delete_image_files(doomed)
                return self._send(200, {"ok": True, "id": target_id,
                                        "purged": True, "images_removed": removed})

            return self._send(400, {"ok": False, "error": f"未知操作: {action}"})
        finally:
            conn.close()


def mask_key(k: str) -> str:
    if not k:
        return ""
    return k[:5] + "***" + k[-4:] if len(k) > 12 else "***"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    created = ensure_schema()
    if created:
        name, pw = created
        bar = "=" * 62
        print(bar, file=sys.stderr)
        print("  首次部署：已自动创建默认管理员（否则没有账号能登录进来）", file=sys.stderr)
        print(f"  用户名: {name}", file=sys.stderr)
        print(f"  密  码: {pw}", file=sys.stderr)
        print("  以上信息同时写入 data/INITIAL_ADMIN.txt（权限 0600）", file=sys.stderr)
        print("  登录后请立刻修改密码，并删除该文件。", file=sys.stderr)
        print(bar, file=sys.stderr)
    print(f"shoppinglog-api v{version_info.get_version()} listening on 127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
