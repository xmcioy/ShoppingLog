#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
购物记录知识库 · 统一管理脚本

用法:
  python3 shop.py add    --name "XXX" --price 199 --category 衣服 [选项...]
  python3 shop.py list   [--category 食物] [--days 30] [--limit 20]
  python3 shop.py search --q "关键词"
  python3 shop.py stats
  python3 shop.py report          # 重新生成 dashboard.html
  python3 shop.py export [--csv 路径]
  python3 shop.py delete --id 3   # 或 --all --yes
  python3 shop.py categories
"""

import argparse
import csv
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime, date, timedelta
from pathlib import Path

import db

ROOT = Path(os.environ.get("SHOPLOG_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "data"
IMG_DIR = ROOT / "images"
EXPORT_DIR = ROOT / "exports"
DB_PATH = DATA_DIR / "shopping.db"
DASHBOARD = ROOT / "dashboard.html"
TEMPLATE = Path(__file__).resolve().parent / "template.html"
# 源码目录（与运行目录 ROOT 分离）：表结构定义文件的唯一权威源在此
SRC_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL_FILE = SRC_ROOT / "docs" / "schema.sql"

DEFAULT_CATEGORIES = [
    "食物", "衣服", "鞋", "包配", "美妆护肤", "数码家电",
    "家居日用", "母婴", "运动户外", "图书文具", "药品保健",
    "餐饮", "宠物", "其他",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_date VARCHAR(10) NOT NULL,
    category      VARCHAR(32),
    subcategory   TEXT,
    name          TEXT NOT NULL,
    brand         TEXT,
    price         REAL,
    currency      VARCHAR(8) DEFAULT 'CNY',
    quantity      REAL DEFAULT 1,
    channel       TEXT,
    place         TEXT,
    rating        REAL,
    original_price REAL,
    paid_price     REAL,
    gift_card_value REAL,
    jd_bean_value  REAL,
    unit_price    REAL,
    shelf_life    TEXT,
    produced_date TEXT,
    expiry_date   TEXT,
    review        TEXT,
    tags          TEXT,
    repurchase    TEXT,
    images        TEXT,
    source        TEXT,
    ingredients   TEXT,
    nutrition     TEXT,
    nutrition_analysis TEXT,
    manufacturer      TEXT,
    manufacturer_address TEXT,
    manufacturer_phone   TEXT,
    tips              TEXT,
    allergens         TEXT,
    license           TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(purchase_date);
CREATE INDEX IF NOT EXISTS idx_items_cat  ON items(category);
"""

# items 主表相对内嵌 SCHEMA 缺失的列（schema.sql 中已定义，此处补齐以保持两处一致）。
# 权威源是 docs/schema.sql；本表仅用于旧库升级时补列，不再用于建表。
SCHEMA_ONLY_COLUMNS = [
    ("user_id", "INTEGER"),
    ("health_score", "REAL"),
    ("health_level", "TEXT"),
    ("health_reason", "TEXT"),
    ("purchase_history", "TEXT"),
    ("product_group", "TEXT"),
    ("flavor_name", "TEXT"),
]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def ensure_dirs():
    for d in (ROOT, DATA_DIR, IMG_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


EXTRA_COLUMNS = [
    ("original_price", "REAL"),
    ("paid_price", "REAL"),
    ("gift_card_value", "REAL"),
    ("jd_bean_value", "REAL"),
    ("unit_price", "REAL"),
    ("purchase_history", "TEXT"),
    ("product_group", "TEXT"),
    ("flavor_name", "TEXT"),
]

# ---- 低耦合分表设计 ----
# 公共字段（所有品类共有，保留在 items 主表）
COMMON_FIELDS = [
    "id", "purchase_date", "category", "subcategory", "name", "brand", "price",
    "currency", "quantity", "channel", "place", "rating", "review", "tags",
    "repurchase", "images", "source", "created_at", "original_price", "paid_price",
    "gift_card_value", "jd_bean_value", "unit_price", "purchase_history",
    "product_group", "flavor_name",
]

# 品类 → 扩展表路由（同属性品类共表；无扩展属性的品类只用主表）
CAT_TO_EXT = {
    "食物": "food_items", "食品": "food_items", "餐饮": "food_items",
    "药品保健": "health_items",
    "美妆护肤": "beauty_items",
    "衣服": "clothing_items", "鞋": "clothing_items", "包配": "clothing_items",
    "运动户外": "clothing_items", "母婴": "clothing_items",
    "数码家电": "electronics_items",
}

# 扩展表结构：同一张表只放属性相同的品类字段（item_id 关联主表，1:1）
EXT_SCHEMAS = {
    "food_items": {
        "shelf_life": "TEXT", "produced_date": "TEXT", "expiry_date": "TEXT",
        "eating_method": "TEXT", "image_desc": "TEXT", "ingredients": "TEXT",
        "nutrition": "TEXT", "nutrition_analysis": "TEXT", "manufacturer": "TEXT",
        "manufacturer_address": "TEXT", "manufacturer_phone": "TEXT", "tips": "TEXT",
        "allergens": "TEXT", "license": "TEXT", "standard_no": "TEXT",
        "entrusting_party": "TEXT", "entrusting_address": "TEXT", "origin": "TEXT",
        "storage_condition": "TEXT", "service_hotline": "TEXT", "specs": "TEXT",
        "flavors": "TEXT",
    },
    "health_items": {
        "shelf_life": "TEXT", "produced_date": "TEXT", "expiry_date": "TEXT",
        "manufacturer": "TEXT", "manufacturer_address": "TEXT", "manufacturer_phone": "TEXT",
        "tips": "TEXT", "license": "TEXT", "origin": "TEXT", "storage_condition": "TEXT",
        "service_hotline": "TEXT", "specs": "TEXT", "indications": "TEXT", "dosage": "TEXT",
        "contraindications": "TEXT", "adverse_reactions": "TEXT", "cautions": "TEXT",
        "approval_no": "TEXT",
    },
    "beauty_items": {
        "shelf_life": "TEXT", "produced_date": "TEXT", "expiry_date": "TEXT",
        "ingredients": "TEXT", "manufacturer": "TEXT", "manufacturer_address": "TEXT",
        "manufacturer_phone": "TEXT", "tips": "TEXT", "allergens": "TEXT",
        "license": "TEXT", "origin": "TEXT",
        "storage_condition": "TEXT", "service_hotline": "TEXT", "specs": "TEXT",
        "eating_method": "TEXT", "standard_no": "TEXT", "image_desc": "TEXT",
    },
    "clothing_items": {
        "size": "TEXT", "color": "TEXT", "material": "TEXT", "care": "TEXT",
        "style": "TEXT", "season": "TEXT",
    },
    "electronics_items": {
        "model": "TEXT", "specs": "TEXT", "warranty": "TEXT", "serial": "TEXT",
    },
}
EXT_ALL_FIELDS = sorted({c for cols in EXT_SCHEMAS.values() for c in cols})
EXT_TABLES = tuple(EXT_SCHEMAS.keys())


def ext_join_sql(alias="items"):
    """生成 LEFT JOIN 扩展表与带表前缀的投影列（避免跨表同名列冲突），返回 (join_sql, col_expr)"""
    joins = " ".join(f"LEFT JOIN {t} ON {t}.item_id = {alias}.id" for t in EXT_SCHEMAS)
    cols = ", ".join(f"{t}.{c} AS {t}__{c}" for t in EXT_SCHEMAS for c in EXT_SCHEMAS[t])
    return joins, cols


def merged_dict(row):
    """将含表前缀别名的查询行合并为业务字段 dict：主表字段为基础，扩展表非 NULL 值覆盖同名业务字段；扩展字段键始终存在（无值补 None）。"""
    ext_keys = {c for t in EXT_SCHEMAS for c in EXT_SCHEMAS[t]}
    d = {k: v for k, v in zip(row.keys(), row) if "__" not in k}
    for k in ext_keys:
        d.setdefault(k, None)
    for k in row.keys():
        if "__" in k:
            _, c = k.split("__", 1)
            v = row[k]
            if v is not None:
                d[c] = v
    return d


def connect():
    ensure_dirs()
    conn = db.connect()
    # 建表：唯一权威源是 docs/schema.sql（源码目录），不再使用内嵌 SCHEMA 副本。
    # 权威文件缺失时回退到内嵌 SCHEMA，保证 CLI 在裁剪部署下仍可用。
    if SCHEMA_SQL_FILE.exists():
        conn.executescript(SCHEMA_SQL_FILE.read_text(encoding="utf-8"))
    else:
        conn.executescript(SCHEMA)
    # 补列：把 schema.sql 中有、内嵌 SCHEMA 中没有的列一并补上（旧库升级）
    existing = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, ddl in EXTRA_COLUMNS + SCHEMA_ONLY_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
    for t, cols in EXT_SCHEMAS.items():
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {t} ("
            f"item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE, "
            f"{', '.join(f'{c} {d}' for c, d in cols.items())})"
        )
        tex = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        for col, ddl in cols.items():
            if col not in tex:
                conn.execute(f"ALTER TABLE {t} ADD COLUMN {col} {ddl}")
    conn.commit()
    return conn


# 营养成分键名别名（英文键为标准存储格式）
NUTRI_ALIAS = {
    "能量": "energy_kj", "energy": "energy_kj", "energy_kj": "energy_kj", "热量": "energy_kj",
    "蛋白质": "protein_g", "protein": "protein_g", "protein_g": "protein_g",
    "脂肪": "fat_g", "fat": "fat_g", "fat_g": "fat_g",
    "反式脂肪酸": "trans_fat_g", "trans_fat": "trans_fat_g", "trans_fat_g": "trans_fat_g",
    "碳水化合物": "carbs_g", "碳水": "carbs_g", "carbs": "carbs_g", "carbs_g": "carbs_g",
    "糖": "sugar_g", "sugar": "sugar_g", "sugar_g": "sugar_g",
    "膳食纤维": "fiber_g", "fiber": "fiber_g", "fiber_g": "fiber_g",
    "钠": "sodium_mg", "sodium": "sodium_mg", "sodium_mg": "sodium_mg",
    "钙": "calcium_mg", "calcium": "calcium_mg", "calcium_mg": "calcium_mg",
}
NUTRI_UNIT = {
    "energy_kj": "千焦", "protein_g": "克", "fat_g": "克", "trans_fat_g": "克",
    "carbs_g": "克", "sugar_g": "克", "fiber_g": "克", "sodium_mg": "毫克", "calcium_mg": "毫克",
}


def parse_flavors(text):
    """接受 JSON 数组 或 逗号/顿号分隔文本，返回口味名称列表；无法解析时按原文本单元素返回"""
    if not text:
        return None
    text = text.strip()
    if text.startswith("["):
        try:
            raw = json.loads(text)
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    parts = re.split(r"[，,、；;]", text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts or [text]


def parse_nutrition(text):
    """接受 JSON 或 '能量=1484,蛋白质=9.0,脂肪=0.6,...' 形式的字符串（均为每 100g 数值）"""
    if not text:
        return None
    text = text.strip()
    if text.startswith("{"):
        raw = json.loads(text)
        return {str(k): (None if v in ("", None) else float(v)) for k, v in raw.items()}
    out = {}
    for part in re.split(r"[,;，；\n]", text):
        if "=" not in part and ":" not in part:
            continue
        k, v = re.split(r"[=:]", part, maxsplit=1)
        key = NUTRI_ALIAS.get(k.strip().lower(), NUTRI_ALIAS.get(k.strip()))
        if not key:
            continue
        m = re.search(r"-?\d+(?:\.\d+)?", v)
        out[key] = float(m.group()) if m else None
    return out or None


def calc_expiry_date(produced_date, shelf_life):
    """按生产日期+保质期计算到期日期。保质期支持 X天/X个月/X年，如 '15天' '150天' '6个月' '1年'。"""
    if not produced_date or not shelf_life:
        return None
    try:
        d = datetime.strptime(produced_date, "%Y-%m-%d").date()
    except Exception:
        return None
    m = re.match(r"^\s*(\d+)\s*(天|日|个月|月|年)", str(shelf_life).strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit in ("天", "日"):
        return (d + timedelta(days=n)).strftime("%Y-%m-%d")
    if unit in ("个月", "月"):
        month_idx = d.month - 1 + n
        return (d.replace(year=d.year + month_idx // 12, month=month_idx % 12 + 1)).strftime("%Y-%m-%d")
    if unit == "年":
        return (d.replace(year=d.year + n)).strftime("%Y-%m-%d")
    return None


def resolve_unit_price(price, quantity, unit_price):
    """单价优先级：显式传 unit_price 优先；否则 price 即单价（quantity<=1）；多件且未传则返回 None 待用户显式指定。"""
    if unit_price is not None:
        return float(unit_price)
    if price is None:
        return None
    q = int(quantity or 1)
    return float(price) if q <= 1 else None


def row_to_dict(r):
    d = dict(r)
    try:
        d["images"] = json.loads(d.get("images") or "[]")
    except Exception:
        d["images"] = []
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t.strip()]
    try:
        d["nutrition"] = json.loads(d.get("nutrition") or "null")
    except Exception:
        d["nutrition"] = None
    price, qty = d.get("price"), d.get("quantity") or 1
    if d.get("unit_price") is None:
        d["unit_price"] = round(price / qty, 2) if price and qty else None
    return d


def safe_name(text, maxlen=24):
    text = unicodedata.normalize("NFKC", str(text)).strip()
    keep = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            keep.append(ch)
        elif ch in " -－—":
            keep.append("-")
    out = "".join(keep).strip("-")[:maxlen]
    return out or "item"


def store_images(paths, purchase_date, name):
    """把图片复制到 images/YYYY-MM/ 下，返回相对 ROOT 的路径列表"""
    saved = []
    target_dir = IMG_DIR / purchase_date[:7]
    target_dir.mkdir(parents=True, exist_ok=True)
    base = f"{purchase_date}_{safe_name(name)}"
    for i, p in enumerate(paths):
        src = Path(p).expanduser()
        if not src.exists():
            print(f"[warn] 图片不存在，已跳过: {p}", file=sys.stderr)
            continue
        ext = src.suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"):
            ext = ".jpg"
        dest = target_dir / f"{base}_{i + 1}{ext}"
        n = 1
        while dest.exists():
            dest = target_dir / f"{base}_{i + 1}_{n}{ext}"
            n += 1
        shutil.copy2(src, dest)
        saved.append(str(dest.relative_to(ROOT)))
    return saved


def fmt_money(v, currency="CNY"):
    if v is None:
        return "-"
    sym = {"CNY": "¥", "JPY": "¥", "USD": "$", "EUR": "€", "HKD": "HK$"}.get(currency, currency + " ")
    return f"{sym}{v:,.2f}"


def stars(n):
    if not n:
        return "—"
    full = int(n)
    half = 1 if n - full >= 0.25 else 0
    return "★" * full + ("½" if half else "") + "☆" * (5 - full - half)


def validate_rating(n):
    """评分需在 1~5 之间，且为 0.5 的整数倍（支持半星）。"""
    if not (1 <= n <= 5):
        sys.exit("[error] --rating 需在 1~5 之间")
    if abs(n * 2 - round(n * 2)) > 1e-9:
        sys.exit("[error] --rating 仅支持 0.5 步进（如 2.5、4.5）")


def print_table(rows, headers, keys, widths=None):
    cols = list(zip(headers, keys))
    data = [[("" if r.get(k) is None else str(r.get(k))) for _, k in cols] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in data)) if data else len(h) for i, (h, _) in enumerate(cols)]
    line = "  ".join(h.ljust(w) for (h, _), w in zip(cols, widths))
    print(line)
    print("-" * len(line))
    for r in data:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------
def cmd_add(args):
    conn = connect()
    pdate = args.date or date.today().isoformat()
    try:
        datetime.strptime(pdate, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"[error] --date 需为 YYYY-MM-DD，收到: {pdate}")

    if args.rating is not None:
        validate_rating(args.rating)

    images = store_images(args.image or [], pdate, args.name)
    now = datetime.now().isoformat(timespec="seconds")
    nut = parse_nutrition(args.nutrition)
    unit_price = resolve_unit_price(args.price, args.quantity, args.unit_price)
    expiry = args.expiry_date or calc_expiry_date(args.produced_date, args.shelf_life)
    flavors = json.dumps(parse_flavors(args.flavors), ensure_ascii=False) if args.flavors else None
    common_cols = ["purchase_date", "category", "subcategory", "name", "brand", "price",
                   "currency", "quantity", "channel", "place", "rating", "original_price",
                   "paid_price", "gift_card_value", "jd_bean_value", "unit_price",
                   "review", "tags", "repurchase", "images", "source",
                   "purchase_history", "product_group", "flavor_name", "created_at",
                   "user_id"]
    common_vals = [pdate, args.category, args.subcategory, args.name, args.brand, args.price,
                   args.currency, args.quantity, args.channel, args.place, args.rating,
                   args.original_price, args.paid_price, args.gift_card_value,
                   args.jd_bean_value, unit_price, args.review, args.tags, args.repurchase,
                   json.dumps(images, ensure_ascii=False), args.source,
                   args.purchase_history, args.product_group, args.flavor_name, now,
                   _CUR_USER_ID]
    cur = conn.execute(
        f"INSERT INTO items ({', '.join(common_cols)}) VALUES ({','.join('?' * len(common_cols))})",
        common_vals,
    )
    # 扩展表：按品类路由写入品类特有字段（同一张表只放属性相同的品类）
    ext = CAT_TO_EXT.get(args.category)
    if ext:
        ev = {}
        for c in EXT_SCHEMAS[ext]:
            if c == "expiry_date":
                v = expiry
            elif c == "nutrition":
                v = json.dumps(nut, ensure_ascii=False) if nut else None
            elif c == "flavors":
                v = flavors
            else:
                v = getattr(args, c, None)
            if v is not None:
                ev[c] = v
        if ev:
            conn.execute(
                f"INSERT INTO {ext} (item_id, {','.join(ev.keys())}) "
                f"VALUES (?, {','.join('?' * len(ev))})",
                [cur.lastrowid, *ev.values()],
            )
    conn.commit()
    rec = row_to_dict(conn.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone())
    conn.close()
    print(f"✅ 已记录 #{rec['id']}  {rec['name']}  {fmt_money(rec['price'], rec['currency'])}"
          f"  [{rec['category']}]  {stars(rec['rating'])}")
    for p in rec["images"]:
        print(f"   图片: {ROOT / p}")
    cmd_report(argparse.Namespace(quiet=True))
    return rec


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def fetch(args=None, where=None, params=None, order="purchase_date DESC, id DESC", limit=None):
    conn = connect()
    joins, ext_cols = ext_join_sql()
    sql = f"SELECT items.*, {ext_cols} FROM items {joins}"
    conds, ps = [], []
    scope = user_scope()
    if scope:
        conds.append(scope[0])
        ps.extend(scope[1])
    if where:
        conds.append(where)
        ps.extend(params or [])
    if args is not None:
        if getattr(args, "category", None):
            conds.append("category = ?")
            ps.append(args.category)
        if getattr(args, "days", None):
            conds.append("purchase_date >= date('now', ?)")
            ps.append(f"-{args.days} days")
        if getattr(args, "month", None):
            conds.append("substr(purchase_date,1,7) = ?")
            ps.append(args.month)
        if getattr(args, "q", None):
            q = f"%{args.q}%"
            conds.append("(name LIKE ? OR brand LIKE ? OR review LIKE ? OR tags LIKE ? OR channel LIKE ?)")
            ps.extend([q, q, q, q, q])
        if getattr(args, "min_price", None) is not None:
            conds.append("price >= ?")
            ps.append(args.min_price)
        if getattr(args, "max_price", None) is not None:
            conds.append("price <= ?")
            ps.append(args.max_price)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {limit}"
    rows = [merged_dict(r) for r in conn.execute(sql, ps).fetchall()]
    conn.close()
    return rows


def cmd_list(args):
    rows = fetch(args, limit=args.limit)
    if not rows:
        print("（无匹配记录）")
        return
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print_table(rows, ["ID", "日期", "分类", "名称", "品牌", "价格", "评分", "渠道"],
                ["id", "purchase_date", "category", "name", "brand", "price", "rating", "channel"])
    print(f"\n共 {len(rows)} 条 · 合计 {fmt_money(sum(r['price'] or 0 for r in rows))}")


def cmd_search(args):
    cmd_list(args)


def cmd_stats(args):
    conn = connect()
    scope = user_scope()
    sql = "SELECT * FROM items"
    params = []
    if scope:
        sql += f" WHERE {scope[0]}"
        params = list(scope[1])
    rows = [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    if not rows:
        print("（还没有任何记录）")
        return
    total = sum(r["price"] or 0 for r in rows)
    print(f"总支出 {fmt_money(total)} · 共 {len(rows)} 件 · 均价 {fmt_money(total / len(rows))}")
    rated = [r["rating"] for r in rows if r["rating"]]
    if rated:
        print(f"平均评分 {sum(rated) / len(rated):.2f} / 5  （{len(rated)} 件已评分）")
    dates = sorted(r["purchase_date"] for r in rows)
    print(f"时间范围 {dates[0]} ~ {dates[-1]}")

    print("\n【分类支出】")
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r["category"] or "其他", []).append(r)
    for cat, items in sorted(by_cat.items(), key=lambda kv: -sum(i["price"] or 0 for i in kv[1])):
        s = sum(i["price"] or 0 for i in items)
        pct = s / total * 100 if total else 0
        print(f"  {cat:<8} {fmt_money(s):>12}  {pct:5.1f}%  ({len(items)} 件)")

    print("\n【月度支出】")
    by_month = {}
    for r in rows:
        by_month.setdefault(r["purchase_date"][:7], []).append(r["price"] or 0)
    mx = max((sum(v) for v in by_month.values()), default=0)
    for m in sorted(by_month):
        s = sum(by_month[m])
        bar = "█" * int(round(s / mx * 30)) if mx else ""
        print(f"  {m}  {fmt_money(s):>12}  {bar}")

    print("\n【回购意愿】")
    rp = {}
    for r in rows:
        rp[r["repurchase"] or "未标注"] = rp.get(r["repurchase"] or "未标注", 0) + 1
    for k, v in rp.items():
        print(f"  {k}: {v} 件")

    print("\n【最贵 5 件】")
    for r in sorted(rows, key=lambda x: -(x["price"] or 0))[:5]:
        print(f"  {fmt_money(r['price']):>12}  {r['name']}  ({r['purchase_date']}, {r['category']}) {stars(r['rating'])}")


# --------------------------------------------------------------------------
# 看板 / 导出 / 删除
# --------------------------------------------------------------------------
def _is_git_tracked(path: Path) -> bool:
    """判断目标文件是否会被 git 追踪（已追踪或未被 ignore 覆盖）。

    看板 HTML 内嵌真实消费记录，一旦进入版本库即等于公开泄露。
    生成前先自检，避免"先生成、后被误提交"。
    """
    try:
        root = Path(__file__).resolve().parent.parent
        rel = path.resolve().relative_to(root)
    except (ValueError, OSError):
        return False  # 不在源码目录内（例如运行目录），无泄露风险
    import subprocess
    try:
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", str(rel)],
            cwd=str(root), capture_output=True, timeout=5)
        if r.returncode == 0:
            return False  # 已被 .gitignore 覆盖
        r2 = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(rel)],
            cwd=str(root), capture_output=True, timeout=5)
        return r2.returncode == 0
    except Exception:
        return False


def cmd_report(args):
    rows = fetch(order="purchase_date DESC, id DESC")
    if not TEMPLATE.exists():
        sys.exit(f"[error] 缺少模板文件: {TEMPLATE}")
    if _is_git_tracked(DASHBOARD):
        sys.exit(
            f"[error] 拒绝生成看板: {DASHBOARD} 会被 git 追踪，"
            f"内含真实消费记录。请在 .gitignore 中忽略该路径，"
            f"或设置 SHOPLOG_ROOT 指向运行目录后再生成。")
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(rows, ensure_ascii=False, indent=1)
    html = html.replace("/*__DATA__*/null", payload)
    html = html.replace("__GENERATED_AT__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    DASHBOARD.write_text(html, encoding="utf-8")
    if not getattr(args, "quiet", False):
        print(f"📊 看板已更新: {DASHBOARD}  （{len(rows)} 条记录）")
    return DASHBOARD


def cmd_export(args):
    rows = fetch()
    if not rows:
        print("（无数据可导出）")
        return
    if args.json:
        p = Path(args.json).expanduser()
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已导出 JSON: {p}")
        return
    p = Path(args.csv or (EXPORT_DIR / f"shopping_{date.today().isoformat()}.csv")).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "purchase_date", "category", "subcategory", "name", "brand", "price",
            "currency", "quantity", "original_price", "paid_price", "gift_card_value",
            "jd_bean_value", "unit_price", "channel", "place", "rating", "review",
            "tags", "repurchase", "ingredients", "nutrition", "nutrition_analysis",
            "eating_method", "image_desc", "manufacturer", "manufacturer_address",
            "manufacturer_phone", "tips", "allergens", "license", "standard_no",
            "entrusting_party", "entrusting_address", "origin", "storage_condition",
            "service_hotline", "images", "source"]
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = merged_dict(r)
            r["tags"] = ",".join(r.get("tags") or [])
            r["images"] = " | ".join(r.get("images") or [])
            if r.get("nutrition"):
                r["nutrition"] = " / ".join(
                    f"{k}={v}{NUTRI_UNIT.get(k, '')}" for k, v in r["nutrition"].items() if v is not None)
            w.writerow(r)
    print(f"已导出 CSV: {p}")


def cmd_update(args):
    conn = connect()
    joins, ext_cols = ext_join_sql()
    scope = user_scope()
    where = "items.id=?"
    params = [args.id]
    if scope:
        where += f" AND {scope[0]}"
        params += list(scope[1])
    row = conn.execute(f"SELECT items.*, {ext_cols} FROM items {joins} WHERE {where}", params).fetchone()
    if not row:
        sys.exit(f"[error] 未找到 #{args.id}")
    if args.rating is not None:
        validate_rating(args.rating)
    old_cat = row["category"] or "其他"
    new_cat = args.category if args.category is not None else old_cat
    old_ext = CAT_TO_EXT.get(old_cat)
    target_ext = CAT_TO_EXT.get(new_cat)

    common_fields, common_values = [], []
    for f in ["name", "category", "subcategory", "brand", "price", "currency", "quantity",
              "channel", "place", "rating", "original_price", "paid_price", "gift_card_value",
              "jd_bean_value", "review", "tags", "repurchase", "source", "purchase_date",
              "unit_price", "purchase_history", "product_group", "flavor_name"]:
        v = getattr(args, f, None)
        if v is not None:
            common_fields.append(f"{f} = ?")
            common_values.append(v)
    if args.add_image:
        imgs = row_to_dict(row)["images"]
        imgs += store_images(args.add_image, row["purchase_date"], row["name"])
        common_fields.append("images = ?")
        common_values.append(json.dumps(imgs, ensure_ascii=False))

    # 扩展表字段：按品类路由（同一张表只放属性相同的品类）
    ext_vals = {}
    if target_ext:
        for c in EXT_SCHEMAS[target_ext]:
            v = getattr(args, c, None)
            if v is not None:
                ext_vals[c] = v
        if getattr(args, "flavors", None) is not None:
            ext_vals["flavors"] = json.dumps(parse_flavors(args.flavors), ensure_ascii=False)
        if args.expiry_date is not None:
            ext_vals["expiry_date"] = args.expiry_date
        elif (args.produced_date is not None or args.shelf_life is not None):
            prod = args.produced_date if args.produced_date is not None else row["produced_date"]
            shelf = args.shelf_life if args.shelf_life is not None else row["shelf_life"]
            calc = calc_expiry_date(prod, shelf)
            if calc is not None:
                ext_vals["expiry_date"] = calc
        if args.nutrition is not None:
            ext_vals["nutrition"] = json.dumps(parse_nutrition(args.nutrition), ensure_ascii=False)
        if args.nutrition_note is not None:
            ext_vals["nutrition_analysis"] = args.nutrition_note

    if not common_fields and not ext_vals and old_ext == target_ext:
        print("没有需要更新的字段")
        return

    if common_fields:
        common_values.append(args.id)
        if scope:
            common_values += list(scope[1])
        conn.execute(f"UPDATE items SET {', '.join(common_fields)} WHERE id = ?{(' AND ' + scope[0]) if scope else ''}", common_values)
    if target_ext:
        if old_ext and old_ext != target_ext:
            # 品类切换到另一扩展表 → 清理旧扩展表行，避免跨表残留
            conn.execute(f"DELETE FROM {old_ext} WHERE item_id=?", (args.id,))
            if ext_vals:
                conn.execute(
                    f"INSERT INTO {target_ext} (item_id, {','.join(ext_vals.keys())}) "
                    f"VALUES (?, {','.join('?' * len(ext_vals))})",
                    [args.id, *ext_vals.values()],
                )
        elif ext_vals:
            # 同品类 → 增量更新：只写本次指定的字段，其余保留原值
            # （旧实现是无条件 DELETE + 重建，未指定的字段会被清空）
            has_row = conn.execute(
                f"SELECT 1 FROM {target_ext} WHERE item_id=?", (args.id,)).fetchone()
            if has_row:
                sets = ", ".join(f"{c} = ?" for c in ext_vals)
                conn.execute(f"UPDATE {target_ext} SET {sets} WHERE item_id=?",
                             [*ext_vals.values(), args.id])
            else:
                conn.execute(
                    f"INSERT INTO {target_ext} (item_id, {','.join(ext_vals.keys())}) "
                    f"VALUES (?, {','.join('?' * len(ext_vals))})",
                    [args.id, *ext_vals.values()],
                )
    elif old_ext:
        # 品类移出扩展表（如改为"其他"）→ 清理旧扩展表行
        conn.execute(f"DELETE FROM {old_ext} WHERE item_id=?", (args.id,))
    conn.commit()
    conn.close()
    print(f"✏️  已更新 #{args.id}")
    cmd_report(argparse.Namespace(quiet=True))


def cmd_delete(args):
    conn = connect()
    if args.all:
        if not args.yes:
            sys.exit("[error] 清空全部数据需要追加 --yes")
        for t in EXT_SCHEMAS:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='items'")
        conn.commit()
        conn.close()
        print("已清空全部记录")
    elif args.id:
        scope = user_scope()
        cond = "id=?" + (f" AND {scope[0]}" if scope else "")
        params = [args.id] + (list(scope[1]) if scope else [])
        for t in EXT_SCHEMAS:
            conn.execute(f"DELETE FROM {t} WHERE item_id=?", (args.id,))
        conn.execute(f"DELETE FROM items WHERE {cond}", params)
        conn.commit()
        conn.close()
        print(f"已删除 #{args.id}")
    else:
        sys.exit("[error] 请指定 --id 或 --all --yes")
    cmd_report(argparse.Namespace(quiet=True))


def cmd_categories(args):
    for c in DEFAULT_CATEGORIES:
        print(c)


# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="购物记录知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="新增一条购买记录")
    a.add_argument("--name", required=True)
    a.add_argument("--price", required=True, type=float)
    a.add_argument("--category", default="其他")
    a.add_argument("--subcategory")
    a.add_argument("--brand")
    a.add_argument("--date", help="购买日期 YYYY-MM-DD，默认今天")
    a.add_argument("--quantity", type=float, default=1.0)
    a.add_argument("--currency", default="CNY")
    a.add_argument("--channel", help="购买渠道，如 淘宝/京东/山姆/线下店")
    a.add_argument("--place", help="地点/店铺")
    a.add_argument("--rating", type=float, help="满意度 1-5，支持半星（如 2.5）")
    a.add_argument("--original-price", type=float, help="原始价格/标价（如商品总额）")
    a.add_argument("--paid-price", type=float, help="实付价格（最终支付金额）")
    a.add_argument("--gift-card-value", type=float, help="礼品卡抵扣价值（京东等平台）")
    a.add_argument("--jd-bean-value", type=float, help="京豆抵扣价值（京东）")
    a.add_argument("--unit-price", type=float, help="单价（订单详情/包装标注），未传且单件时默认等于 price")
    a.add_argument("--shelf-life", help="保质期，如 '150天' '6个月' '1年'")
    a.add_argument("--produced-date", help="生产日期 YYYY-MM-DD")
    a.add_argument("--expiry-date", help="到期日期 YYYY-MM-DD；未传时按生产日期+保质期自动计算")
    a.add_argument("--eating-method", help="食用方式/使用方法，如 开袋即食/冲泡即饮/微波加热3分钟")
    a.add_argument("--image-desc", help="图片提取的商品信息（名称/类型/规格/介绍等），尽量全量记录")
    a.add_argument("--manufacturer", help="生产商/制造商/代工厂，如 福建酷球食品有限公司")
    a.add_argument("--manufacturer-address", help="生产商地址")
    a.add_argument("--manufacturer-phone", help="生产商电话")
    a.add_argument("--tips", help="温馨提示/食用提示（包装上的注意事项）")
    a.add_argument("--allergens", help="过敏原/致敏物质提示，如 含大豆制品成分")
    a.add_argument("--license", help="食品安全许可证/生产许可证号（如 SC 编号）")
    a.add_argument("--standard-no", help="产品标准号，如 GB 19295")
    a.add_argument("--entrusting-party", help="委托方/品牌方名称")
    a.add_argument("--entrusting-address", help="委托方/品牌方地址")
    a.add_argument("--origin", help="产地，如 江苏省扬州市")
    a.add_argument("--storage-condition", help="贮存条件，如 -18℃冷冻保存")
    a.add_argument("--service-hotline", help="全国服务热线/客服电话")
    a.add_argument("--review", help="评价文字")
    a.add_argument("--tags", help="逗号分隔标签，如 回购,性价比高")
    a.add_argument("--repurchase", choices=["yes", "no", "maybe"], help="是否回购")
    a.add_argument("--image", action="append", help="图片路径，可重复传入")
    a.add_argument("--source", help="来源说明，如 订单截图/聊天记录")
    a.add_argument("--flavors", help="口味/风味信息，JSON 数组或逗号分隔文本，如 '[\"经典提拉米苏\",\"开心果巴斯克\"]'；多口味/拼装食品（图片可读）必填")
    a.add_argument("--ingredients", help="配料表原文，如 '小麦粉、白砂糖、乳粉≥3%%、山药粉≥2%%'")
    a.add_argument("--nutrition", help="营养成分（均为每100g），如 '能量=1484,蛋白质=9.0,脂肪=0.6,碳水=77.0,钠=164,钙=125' 或 JSON")
    a.add_argument("--nutrition-note", help="营养价值分析文字")
    a.add_argument("--specs", help="电子产品配置，如 'CPU=M2,内存=16G,存储=512G,屏幕=14英寸'")
    a.add_argument("--indications", help="药品适应症")
    a.add_argument("--dosage", help="药品服用方法/用法用量")
    a.add_argument("--contraindications", help="药品禁忌")
    a.add_argument("--adverse-reactions", help="药品不良反应")
    a.add_argument("--cautions", help="注意事项")
    a.add_argument("--approval-no", help="批准文号，如 国药准字Hxxxxx / 医疗器械注册证号")
    a.add_argument("--purchase-history", help="多次买入记录 JSON 或文本，如 '[{\"date\":\"2026-10-01\",\"price\":19.56,\"original_price\":29.9,\"paid_price\":0,\"produced_date\":\"2026-09-01\",\"expiry_date\":\"2027-09-01\"}]'")
    a.add_argument("--product-group", help="同产品不同口味关联组ID：同一产品的多个口味条目共享同一ID，用于网页 tab 聚合展示（如 brand_series_YYYYMMDD）")
    a.add_argument("--flavor-name", help="本条记录对应的口味名称，同产品多口味拆分时必填，作为网页 tab 标签（如 口味A/口味B）")
    a.add_argument("--size", help="服装尺码，如 M/170/92A")
    a.add_argument("--color", help="服装颜色/花色")
    a.add_argument("--material", help="面料材质，如 100%棉/聚酯纤维")
    a.add_argument("--care", help="洗涤/保养方式")
    a.add_argument("--style", help="款式风格，如 宽松/修身/工装")
    a.add_argument("--season", help="适用季节")
    a.add_argument("--model", help="数码产品型号，如 MacBook Pro 14")
    a.add_argument("--warranty", help="保修信息，如 全国联保2年")
    a.add_argument("--serial", help="序列号/SN")
    a.set_defaults(func=cmd_add)

    u = sub.add_parser("update", help="修改已有记录（只改传入的字段）")
    u.add_argument("--id", type=int, required=True)
    u.add_argument("--name")
    u.add_argument("--category")
    u.add_argument("--subcategory")
    u.add_argument("--brand")
    u.add_argument("--price", type=float)
    u.add_argument("--currency")
    u.add_argument("--quantity", type=float)
    u.add_argument("--channel")
    u.add_argument("--place")
    u.add_argument("--date", dest="purchase_date")
    u.add_argument("--rating", type=float)
    u.add_argument("--original-price", type=float)
    u.add_argument("--paid-price", type=float)
    u.add_argument("--gift-card-value", type=float)
    u.add_argument("--jd-bean-value", type=float)
    u.add_argument("--unit-price", type=float, help="单价")
    u.add_argument("--shelf-life", help="保质期，如 '150天'")
    u.add_argument("--produced-date", help="生产日期 YYYY-MM-DD")
    u.add_argument("--expiry-date", help="到期日期 YYYY-MM-DD；不传时按生产日期+保质期重算")
    u.add_argument("--eating-method", help="食用方式/使用方法")
    u.add_argument("--image-desc", help="图片提取的商品信息")
    u.add_argument("--manufacturer", help="生产商/制造商/代工厂")
    u.add_argument("--manufacturer-address", help="生产商地址")
    u.add_argument("--manufacturer-phone", help="生产商电话")
    u.add_argument("--tips", help="温馨提示/食用提示")
    u.add_argument("--allergens", help="过敏原/致敏物质提示")
    u.add_argument("--license", help="食品安全许可证/生产许可证号")
    u.add_argument("--standard-no", help="产品标准号")
    u.add_argument("--entrusting-party", help="委托方/品牌方名称")
    u.add_argument("--entrusting-address", help="委托方/品牌方地址")
    u.add_argument("--origin", help="产地")
    u.add_argument("--storage-condition", help="贮存条件")
    u.add_argument("--service-hotline", help="全国服务热线/客服电话")
    u.add_argument("--specs", help="电子产品配置")
    u.add_argument("--indications", help="药品适应症")
    u.add_argument("--dosage", help="药品服用方法/用法用量")
    u.add_argument("--contraindications", help="药品禁忌")
    u.add_argument("--adverse-reactions", help="药品不良反应")
    u.add_argument("--cautions", help="注意事项")
    u.add_argument("--approval-no", help="批准文号")
    u.add_argument("--purchase-history", help="多次买入记录 JSON")
    u.add_argument("--flavors", help="口味/风味信息，JSON 数组或逗号分隔文本")
    u.add_argument("--product-group", help="同产品不同口味关联组ID")
    u.add_argument("--flavor-name", help="本条记录对应的口味名称")
    u.add_argument("--size", help="服装尺码")
    u.add_argument("--color", help="服装颜色/花色")
    u.add_argument("--material", help="面料材质")
    u.add_argument("--care", help="洗涤/保养方式")
    u.add_argument("--style", help="款式风格")
    u.add_argument("--season", help="适用季节")
    u.add_argument("--model", help="数码产品型号")
    u.add_argument("--warranty", help="保修信息")
    u.add_argument("--serial", help="序列号/SN")
    u.add_argument("--review")
    u.add_argument("--tags")
    u.add_argument("--repurchase", choices=["yes", "no", "maybe"])
    u.add_argument("--source")
    u.add_argument("--ingredients")
    u.add_argument("--nutrition")
    u.add_argument("--nutrition-note")
    u.add_argument("--add-image", action="append", help="追加图片路径，可重复传入")
    u.set_defaults(func=cmd_update)

    for name in ("list", "search"):
        s = sub.add_parser(name, help="查询记录")
        s.add_argument("--category")
        s.add_argument("--days", type=int)
        s.add_argument("--month", help="YYYY-MM")
        if name == "search":
            s.add_argument("--q", required=True, help="关键词：匹配名称/品牌/评价/标签/渠道")
        else:
            s.add_argument("--q")
        s.add_argument("--min-price", type=float)
        s.add_argument("--max-price", type=float)
        s.add_argument("--limit", type=int, default=50)
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=cmd_list if name == "list" else cmd_search)

    sub.add_parser("stats", help="统计概览").set_defaults(func=cmd_stats)
    sub.add_parser("report", help="重新生成看板").set_defaults(func=cmd_report)
    sub.add_parser("categories", help="列出默认分类").set_defaults(func=cmd_categories)

    e = sub.add_parser("export", help="导出 CSV/JSON")
    e.add_argument("--csv")
    e.add_argument("--json")
    e.set_defaults(func=cmd_export)

    d = sub.add_parser("delete", help="删除记录")
    d.add_argument("--id", type=int)
    d.add_argument("--all", action="store_true")
    d.add_argument("--yes", action="store_true")
    d.set_defaults(func=cmd_delete)

    # 0.1 多用户：所有子命令支持 --token（本机用户隔离）与 --api（远端 HTTP 转发）
    for _name, _p in sub.choices.items():
        _p.add_argument("--token", help="用户 Token（设置页获取；也可用环境变量 SHOPPING_TOKEN）")
        _p.add_argument("--api", help="远端 API 地址，如 http://localhost:8088（也可用环境变量 SHOPPING_API）")
    return p


# --------------------------------------------------------------------------
# 0.1 多用户：token 解析与用户隔离
# --------------------------------------------------------------------------
_CUR_USER_ID = None


def set_current_user(token):
    """校验 token → 设置全局 _CUR_USER_ID。无 token 时保持旧模式（仅本机管理）。"""
    global _CUR_USER_ID
    if not token:
        print("[warn] 未指定 --token：将以本机旧模式操作（无用户隔离），仅限本机管理", file=sys.stderr)
        _CUR_USER_ID = None
        return
    conn = db.connect()
    try:
        r = conn.execute("SELECT id FROM users WHERE token=?", (token,)).fetchone()
    finally:
        conn.close()
    if not r:
        sys.exit("[error] Token 无效：请在设置页获取/重置用户 Token")
    _CUR_USER_ID = r[0]


def user_scope(alias="items"):
    """返回 (where_sql, params)；无用户隔离时返回 None"""
    if _CUR_USER_ID is None:
        return None
    return f"{alias}.user_id = ?", (_CUR_USER_ID,)


def api_dispatch(args, api, token):
    """远端模式：将 add/update/delete/list 转发到 server.py API（0.1）"""
    import json as _json
    import urllib.error
    import urllib.request

    base = api.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def call(method, path, payload=None):
        data = _json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return _json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return _json.loads(body)
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}: {body[:200]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    cmd = args.cmd
    if cmd == "add":
        payload = {k: v for k, v in vars(args).items()
                   if v is not None and k not in ("func", "cmd", "token", "api", "image")}
        if args.image:
            print("[warn] 远端模式暂不支持 --image，请改用网页图文上传", file=sys.stderr)
        r = call("POST", "/api/add", payload)
    elif cmd == "update":
        payload = {k: v for k, v in vars(args).items()
                   if v is not None and k not in ("func", "cmd", "token", "api", "add_image")}
        if args.add_image:
            print("[warn] 远端模式暂不支持 --add-image，请改用网页图文上传", file=sys.stderr)
        r = call("POST", "/api/update", payload)
    elif cmd == "delete":
        if args.all:
            return print("[error] 远端模式不支持清空全部；请逐个删除或本机操作")
        r = call("POST", "/api/delete", {"id": args.id})
    elif cmd == "list":
        r = call("GET", "/api/items")
        if r.get("ok"):
            for it in r["items"]:
                print(f"#{it['id']}  {it['purchase_date']}  {it['name']}  {fmt_money(it['price'], it.get('currency','CNY'))}  [{it.get('category') or '其他'}]")
            return print(f"共 {len(r['items'])} 条")
        return print(_json.dumps(r, ensure_ascii=False))
    else:
        return print(f"[error] 远端模式暂不支持 {cmd} 命令（支持 add/update/delete/list）")
    if r.get("ok"):
        print(f"OK: {cmd} -> {r}")
    else:
        print(f"[error] {r.get('error') or r}")
        sys.exit(1)


def main():
    args = build_parser().parse_args()
    token = getattr(args, "token", None) or os.environ.get("SHOPPING_TOKEN")
    api = getattr(args, "api", None) or os.environ.get("SHOPPING_API")
    if api:
        sys.exit(api_dispatch(args, api, token))
    set_current_user(token)
    args.func(args)


if __name__ == "__main__":
    main()
