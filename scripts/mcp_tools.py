#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShoppingLog MCP 工具集（协议无关的纯逻辑层）。

被两处复用：
  - `scripts/server.py`  → `POST /api/mcp`（MCP 跑在服务进程里，推荐）
  - `scripts/mcp_server.py` → stdio 管道（仅做转发，给只支持 stdio 的客户端）

设计要点：
  - 这里**只做参数整形与结果裁剪**，真正的数据操作全部通过 `api()` 回调走 HTTP，
    也就是和网页用同一条鉴权/校验链路，不会出现"MCP 一套逻辑、网页另一套"的漂移。
  - 进程内**没有数据库连接**，拿不到任何人的数据，除非 Token 允许。

`api` 回调签名：api(method, path, payload=None, timeout=30) -> dict
"""

import base64
import json
import urllib.parse
from pathlib import Path

import health_score
import version as version_info  # 版本号唯一来源：仓库根 VERSION 文件

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "shoppinglog"

def _clean(d):
    """去掉 None 值，避免把「没提供」当成「清空」。"""
    return {k: v for k, v in (d or {}).items() if v is not None}


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------
def t_whoami(api, args):
    me = api("GET", "/me")
    if not me.get("ok"):
        raise RuntimeError(me.get("error") or "读取账号失败")
    return {
        "username": me.get("username"),
        "display_name": me.get("display_name"),
        "is_admin": bool(me.get("is_admin")),
        "created_at": me.get("created_at"),
    }


def t_list_items(api, args):
    d = api("GET", "/items")
    items = d.get("items") or []
    q = (args.get("q") or "").strip().lower()
    cat = (args.get("category") or "").strip()
    frm = (args.get("from_date") or "").strip()
    to = (args.get("to_date") or "").strip()
    grade = (args.get("grade") or "").strip().upper()
    limit = int(args.get("limit") or 50)

    def grade_ok(it):
        """按健康档次筛选：A/B/C/D；none = 还没有健康评分的记录。"""
        if not grade:
            return True
        s = it.get("health_score")
        if grade == "NONE":
            return s is None
        g, _ = health_score.grade_of(s)
        return g == grade

    def keep(it):
        if not grade_ok(it):
            return False
        if cat and it.get("category") != cat:
            return False
        d0 = it.get("purchase_date") or ""
        if frm and d0 < frm:
            return False
        if to and d0 > to:
            return False
        if q:
            blob = " ".join(str(it.get(k) or "") for k in
                            ("name", "brand", "review", "tags", "category", "channel"))
            if q not in blob.lower():
                return False
        return True

    rows = [it for it in items if keep(it)]
    rows.sort(key=lambda x: (x.get("purchase_date") or "", x.get("id") or 0), reverse=True)
    rows = rows[:limit]
    # 只回传对模型有用的字段，避免一次吐几十 KB
    slim = []
    for it in rows:
        slim.append(_clean({
            "id": it.get("id"),
            "name": it.get("name"),
            "price": it.get("price"),
            "purchase_date": it.get("purchase_date"),
            "category": it.get("category"),
            "brand": it.get("brand"),
            "channel": it.get("channel"),
            "place": it.get("place"),
            "quantity": it.get("quantity"),
            "rating": it.get("rating"),
            "review": it.get("review"),
            "tags": it.get("tags"),
            "repurchase": it.get("repurchase"),
            "visibility": it.get("visibility"),
            "images": it.get("images"),
            "health_score": it.get("health_score"),
            "health_level": it.get("health_level"),
            "health_grade": health_score.grade_of(it.get("health_score"))[0],
        }))
    return {"count": len(slim), "total_matched": len([it for it in items if keep(it)]),
            "items": slim}


def t_get_item(api, args):
    iid = int(args["id"])
    d = api("GET", "/items")
    for it in d.get("items") or []:
        if it.get("id") == iid:
            return it
    raise RuntimeError(f"找不到记录 #{iid}（可能不存在，或不属于当前 Token 的账号）")


def t_add_item(api, args):
    item = dict(args.get("item") or {})
    for k, v in args.items():
        if k == "item" or v is None:
            continue
        item.setdefault(k, v)
    # 复购已有商品：传 item_id 即可，只新增一条购买记录，商品档案不重复建
    if item.get("item_id"):
        if item.get("price") is None or item.get("price") == "":
            raise RuntimeError("缺少 price（本次购买价格）")
        res = api("POST", "/add", _clean(item))
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "新增失败")
        return {"id": res.get("id"), "purchase_id": res.get("purchase_id"),
                "reused": True, "ok": True}
    if not item.get("name"):
        raise RuntimeError("缺少 name")
    if item.get("price") is None or item.get("price") == "":
        raise RuntimeError("缺少 price（实际商品价格）")
    # tags 允许数组或逗号串，服务端存逗号分隔文本
    if isinstance(item.get("tags"), list):
        item["tags"] = ",".join(str(x) for x in item["tags"])
    if isinstance(item.get("nutrition"), dict):
        item["nutrition"] = json.dumps(item["nutrition"], ensure_ascii=False)
    if isinstance(item.get("flavors"), list):
        item["flavors"] = json.dumps(item["flavors"], ensure_ascii=False)
    res = api("POST", "/add", _clean(item))
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "新增失败")
    return {"id": res.get("id"), "ok": True}


def t_update_item(api, args):
    iid = args.get("id")
    if not iid:
        raise RuntimeError("缺少 id")
    body = dict(args.get("item") or {})
    for k, v in args.items():
        if k in ("id", "item") or v is None:
            continue
        body.setdefault(k, v)
    body["id"] = int(iid)
    if isinstance(body.get("tags"), list):
        body["tags"] = ",".join(str(x) for x in body["tags"])
    if isinstance(body.get("nutrition"), dict):
        body["nutrition"] = json.dumps(body["nutrition"], ensure_ascii=False)
    if isinstance(body.get("flavors"), list):
        body["flavors"] = json.dumps(body["flavors"], ensure_ascii=False)
    res = api("POST", "/update", body)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "更新失败")
    return {"id": int(iid), "ok": True, "images_removed": res.get("images_removed", 0)}


def t_delete_item(api, args):
    iid = int(args["id"])
    res = api("POST", "/delete", {"id": iid})
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "删除失败")
    return {"id": iid, "deleted": True}


def t_upload_images(api, args):
    paths = args.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        raise RuntimeError("缺少 paths（本地图片绝对路径数组）")
    blobs = []
    for p in paths:
        f = Path(p).expanduser()
        if not f.is_file():
            raise RuntimeError(f"文件不存在: {p}")
        if f.stat().st_size > 15 * 1024 * 1024:
            raise RuntimeError(f"图片过大（>15MB）: {p}")
        blobs.append(base64.b64encode(f.read_bytes()).decode())
    res = api("POST", "/images", {
        "images": blobs,
        "purchase_date": args.get("purchase_date"),
        "name": args.get("name"),
    }, timeout=120)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "上传失败")
    return {"images": res.get("images") or []}


def t_public_feed(api, args):
    qs = {"page": int(args.get("page") or 1), "size": int(args.get("size") or 10)}
    if args.get("q"):
        qs["q"] = args["q"]
    res = api("GET", "/public?" + urllib.parse.urlencode(qs))
    groups = res.get("groups") or []
    out = []
    for g in groups:
        out.append({
            "name": g.get("name"),
            "owner_count": g.get("owner_count"),
            "reviews": [_clean({
                "owner": it.get("owner_name"),
                "rating": it.get("rating"),
                "review": it.get("review"),
                "share_note": it.get("share_note"),
                "health_score": it.get("health_score"),
            }) for it in (g.get("items") or [])],
        })
    return {"total_products": res.get("total"), "page": res.get("page"),
            "pages": res.get("pages"), "groups": out}


def t_stats(api, args):
    d = api("GET", "/items")
    items = d.get("items") or []
    by_cat, by_month = {}, {}
    total = 0.0
    for it in items:
        price = it.get("price") or 0
        try:
            total += float(price)
        except (TypeError, ValueError):
            pass
        c = it.get("category") or "其他"
        by_cat[c] = by_cat.get(c, 0) + 1
        m = (it.get("purchase_date") or "")[:7]
        if m:
            by_month[m] = round(by_month.get(m, 0) + float(price or 0), 2)
    return {
        "records": len(items),
        "total_spend": round(total, 2),
        "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
        "by_month": dict(sorted(by_month.items(), reverse=True)[:12]),
    }


TOOLS = [
    {
        "name": "whoami",
        "description": "查看当前 Token 对应的账号（确认 MCP 鉴权是否配置正确）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_items",
        "description": "查询自己的购物记录。支持关键词、分类、日期区间过滤，按购买日期倒序。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "关键词（匹配名称/品牌/评价/标签/渠道）"},
                "category": {"type": "string", "description": "分类精确匹配，如 食品"},
                "from_date": {"type": "string", "description": "起始日期 YYYY-MM-DD（含）"},
                "to_date": {"type": "string", "description": "结束日期 YYYY-MM-DD（含）"},
                "grade": {"type": "string",
                          "description": "健康档次：A(≥80) / B(60-79) / C(40-59) / D(<40) / none(未评分)"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 50，上限 200"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_item",
        "description": "按 id 读取一条完整记录（含扩展字段）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_item",
        "description": (
            "新增一条购物记录。两种用法："
            "① 新建商品：name 与 price 必填；"
            "② 复购已有商品：传 item_id（可从 list_items 拿到）+ 本次 price/purchase_date，"
            "只新增一条购买记录，商品档案不重复建。"
            "其余字段可选：purchase_date/category/brand/channel/place/quantity/rating/review/tags/"
            "repurchase/visibility/share_note/images(先调 upload_images 拿路径)/"
            "ingredients/nutrition/expiry_date/shelf_life/produced_date/manufacturer/"
            "license/origin/storage_condition/allergens/flavors 等。"
            "无法归类的额外字段放进 item 对象即可透传。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "复购已有商品时传，只新增购买记录"},
                "name": {"type": "string"},
                "price": {"type": "number", "description": "实际商品价格（单件/单份价）"},
                "purchase_date": {"type": "string", "description": "YYYY-MM-DD"},
                "category": {"type": "string"},
                "brand": {"type": "string"},
                "channel": {"type": "string"},
                "place": {"type": "string"},
                "quantity": {"type": "number"},
                "rating": {"type": "number", "description": "1~5，支持 0.5 步进"},
                "review": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "repurchase": {"type": "string", "enum": ["yes", "maybe", "no"]},
                "visibility": {"type": "string", "enum": ["public", "private"]},
                "share_note": {"type": "string"},
                "images": {"type": "array", "items": {"type": "string"},
                           "description": "upload_images 返回的 images 路径"},
                "item": {"type": "object", "description": "其它任意字段，透传给服务端",
                         "additionalProperties": True},
            },
            "required": ["name", "price"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_item",
        "description": "修改已有记录（只改传入的字段，未传的保留原值）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "item": {"type": "object", "additionalProperties": True,
                         "description": "要修改的字段，如 {\"review\":\"...\"}"},
            },
            "required": ["id"],
            "additionalProperties": True,
        },
    },
    {
        "name": "delete_item",
        "description": "删除一条记录（连同它的图片文件）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "upload_images",
        "description": (
            "把本地图片上传到服务并归档，返回 images 路径数组。"
            "拿到后把路径传给 add_item / update_item 的 images 字段。"
            "图片存在服务端，skill 不需要（也不允许）直接往数据目录写文件。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "本地图片绝对路径数组（最多 20 张）"},
                "purchase_date": {"type": "string", "description": "用于归档目录 YYYY-MM-DD"},
                "name": {"type": "string", "description": "用于文件名，可选"},
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    },
    {
        "name": "public_feed",
        "description": "公开广场：按商品分组的公开评价（不含价格/购买日期等购买记录）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer"},
                "size": {"type": "integer"},
                "q": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "stats",
        "description": "统计自己的记录：总数、总花费、分类分布、按月花费。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

HANDLERS = {
    "whoami": t_whoami,
    "list_items": t_list_items,
    "get_item": t_get_item,
    "add_item": t_add_item,
    "update_item": t_update_item,
    "delete_item": t_delete_item,
    "upload_images": t_upload_images,
    "public_feed": t_public_feed,
    "stats": t_stats,
}


# --------------------------------------------------------------------------
# JSON-RPC / MCP
# --------------------------------------------------------------------------

HANDLERS = {
    "whoami": t_whoami,
    "list_items": t_list_items,
    "get_item": t_get_item,
    "add_item": t_add_item,
    "update_item": t_update_item,
    "delete_item": t_delete_item,
    "upload_images": t_upload_images,
    "public_feed": t_public_feed,
    "stats": t_stats,
}


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(req, api):
    """处理一条 JSON-RPC 请求。

    返回响应 dict；**通知类消息返回 None**（调用方不应回包）。
    `api` 是回调，由调用方提供（进程内回环 / 直连）。
    """
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": version_info.get_version()},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return _err(rid, -32602, f"未知工具: {name}")
        try:
            result = fn(api, args)
            return _ok(rid, {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                "isError": False,
            })
        except Exception as e:
            return _ok(rid, {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True,
            })
    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return _ok(rid, {key: []})
    return _err(rid, -32601, f"不支持的方法: {method}")
