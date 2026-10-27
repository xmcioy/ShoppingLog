#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ShoppingLog 集成测试。commit 前必须全绿。

用法：
  python3 scripts/run_tests.py            # 全量
  python3 scripts/run_tests.py -k auth    # 只跑名称含 auth 的用例
  python3 scripts/run_tests.py -v         # 显示每个用例

设计：
  - 每个用例在独立的临时运行目录上起一个真实 HTTP 服务（随机端口），互不干扰
  - 只依赖标准库，不引入 pytest
  - 覆盖：鉴权、密码策略、改密、token 重置、管理员权限、数据隔离、可见性、扩展字段不丢失
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable

sys.path.insert(0, str(SCRIPTS))
import version  # 版本号唯一来源：仓库根 VERSION 文件
import health_score  # 健康评分（纯规则，服务端唯一权威）

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------- 基础设施
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    """在临时运行目录上启动一个真实服务实例。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shoplog_test_"))
        self.app = self.tmp / "app"
        self.app.mkdir(parents=True)
        self.port = free_port()
        self.proc = None

    def __enter__(self):
        env = dict(os.environ, SHOPLOG_ROOT=str(self.app))
        self.proc = subprocess.Popen(
            [PY, "server.py", "--port", str(self.port)],
            cwd=str(SCRIPTS), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # 等端口可用
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1).read()
                return self
            except Exception:
                if self.proc.poll() is not None:
                    out = self.proc.stdout.read().decode("utf-8", "replace")
                    raise RuntimeError(f"服务启动失败:\n{out}")
                time.sleep(0.1)
        raise RuntimeError("服务启动超时")

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, method, path, body=None, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}

    def req_full(self, method, path, body=None, token=None):
        """返回 (status, headers, json)——需要看 Set-Cookie 时用。"""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), {}

    def get(self, path, token=None):
        return self.req("GET", path, None, token)

    def raw_get(self, path, token=None):
        """只关心状态码的原始 GET——图片接口返回二进制，不能按 JSON 解析。"""
        url = f"http://127.0.0.1:{self.port}{path}"
        r = urllib.request.Request(url, method="GET")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                resp.read()
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    def post(self, path, body=None, token=None):
        return self.req("POST", path, body, token)

    # 便捷方法
    def make_user(self, username, password, admin=False):
        """通过真实注册接口造用户；管理员权限用直连库补设。

        不用 db.connect()：模块级 ROOT 在 import 时就固化，切换 SHOPLOG_ROOT
        不生效，会连到错误的库。改用 sqlite3 打开本次运行目录的库。
        """
        import sqlite3
        db_path = self.app / "data" / "shopping.db"
        con = sqlite3.connect(str(db_path))
        try:
            con.execute("INSERT OR REPLACE INTO settings (skey, value) VALUES ('allow_register','1')")
            con.commit()
        finally:
            con.close()
        code, d = self.post("/api/register", {"username": username, "password": password})
        if code != 200:
            raise AssertionError(f"注册 {username} 失败: {code} {d}")
        tok = d["token"]
        if admin:
            con = sqlite3.connect(str(db_path))
            try:
                con.execute("UPDATE users SET is_admin=1 WHERE username=?", (username,))
                con.commit()
            finally:
                con.close()
        return tok

    def make_admin_first(self, username="root", password="Root@12345"):
        """首个用户：注册后提权为管理员。返回 token。"""
        return self.make_user(username, password, admin=True)

    def sql(self, query, params=()):
        """直连本次运行目录的库做断言（绕过 HTTP，验证落库结果）。"""
        import sqlite3
        con = sqlite3.connect(str(self.app / "data" / "shopping.db"))
        try:
            return con.execute(query, params).fetchall()
        finally:
            con.close()

    def uid_of(self, admin_token, username, bin_mode=False):
        path = "/api/users?bin=1" if bin_mode else "/api/users"
        users = self.get(path, admin_token)[1].get("users", [])
        hit = [u["id"] for u in users if u["username"] == username]
        if not hit:
            raise AssertionError(f"找不到用户 {username}（bin={bin_mode}）")
        return hit[0]


def assert_eq(actual, expected, what=""):
    if actual != expected:
        raise AssertionError(f"{what}: 期望 {expected!r}，实际 {actual!r}")


def assert_true(cond, what=""):
    if not cond:
        raise AssertionError(f"{what}: 断言失败")


# ---------------------------------------------------------------- 用例
@case("health 存活")
def t_health(s):
    code, d = s.get("/api/health")
    assert_eq(code, 200, "状态码")
    assert_true(d.get("ok"), "ok 字段")


@case("版本号：/api/version 与仓库 VERSION 一致且免鉴权")
def t_version(s):
    code, d = s.get("/api/version")          # 不带 token：网页右下角徽标靠它取版本
    assert_eq(code, 200, "/api/version 状态码")
    assert_eq(d.get("version"), version.get_version(), "版本号与 VERSION 文件一致")
    code2, d2 = s.get("/api/health")
    assert_eq(code2, 200, "/api/health 状态码")
    assert_eq(d2.get("version"), version.get_version(), "health 版本号与 VERSION 文件一致")


@case("MCP：initialize 版本与 VERSION 一致，无效 Token 返回 401")
def t_mcp_version(s):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}}
    # 无效 Token：必须是 401 JSON，不能被登录页 302 吞掉（nginx auth_request 踩过的坑）
    code, _ = s.post("/api/mcp", init, token="invalid-token")
    assert_eq(code, 401, "无效 Token 调用 /api/mcp 状态码")
    # 有效 Token：serverInfo.version 必须等于仓库 VERSION
    tok = s.make_user("mcp_ver", "Mcpver@12345")
    code, d = s.post("/api/mcp", init, token=tok)
    assert_eq(code, 200, "有效 Token 调用 /api/mcp 状态码")
    assert_eq(d["result"]["serverInfo"]["version"], version.get_version(), "MCP serverInfo 版本")


@case("未带 token 访问受保护接口应 401")
def t_auth_required(s):
    for p in ("/api/items", "/api/me", "/api/settings", "/api/users"):
        code, _ = s.get(p)
        assert_eq(code, 401, f"{p} 状态码")


@case("注册：密码强度不足应被拒")
def t_register_weak(s):
    c = s.make_user("admin_boot", "Admin@12345", admin=True)
    for pw, why in [("123", "太短"), ("12345678", "纯数字"),
                    ("abcdefgh", "纯字母"), ("aaaaaaaa", "重复字符")]:
        code, d = s.post("/api/register", {"username": "u_" + pw, "password": pw})
        assert_eq(code, 400, f"弱密码 {why} 应被拒")
        assert_true(d.get("error"), "应有错误信息")


@case("注册：强密码通过，返回 token")
def t_register_ok(s):
    c = s.make_user("admin_boot2", "Admin@12345", admin=True)
    s.post("/api/settings", {"allow_register": "1"}, c)
    code, d = s.post("/api/register", {"username": "newuser", "password": "Str0ng!pass"})
    assert_eq(code, 200, "注册应成功")
    assert_true(d.get("token"), "应返回 token")


@case("登录：密码错误 401，正确 200 且轮换 token")
def t_login(s):
    s.make_user("alice", "Alice@12345")
    code, _ = s.post("/api/login", {"username": "alice", "password": "wrong"})
    assert_eq(code, 401, "错误密码")
    code, d = s.post("/api/login", {"username": "alice", "password": "Alice@12345"})
    assert_eq(code, 200, "正确密码")
    assert_true(d.get("token"), "应返回 token")


@case("改密：原密码错 403，新密码弱 400，成功后旧 token 失效")
def t_change_password(s):
    tok = s.make_user("bob", "Bob@123456")
    code, _ = s.post("/api/password", {"old_password": "bad", "new_password": "New@12345"}, tok)
    assert_eq(code, 403, "原密码错误")
    code, _ = s.post("/api/password", {"old_password": "Bob@123456", "new_password": "123"}, tok)
    assert_eq(code, 400, "新密码太弱")
    code, d = s.post("/api/password", {"old_password": "Bob@123456", "new_password": "New@12345"}, tok)
    assert_eq(code, 200, "改密应成功")
    new_tok = d.get("token")
    assert_true(new_tok and new_tok != tok, "应返回新 token")
    # 旧 token 失效
    code, _ = s.get("/api/me", tok)
    assert_eq(code, 401, "旧 token 应失效")
    # 新 token 可用
    code, _ = s.get("/api/me", new_tok)
    assert_eq(code, 200, "新 token 应可用")


@case("token 重置：返回新 token 且旧的失效")
def t_reset_token(s):
    tok = s.make_user("carol", "Carol@12345")
    code, d = s.post("/api/token", {}, tok)
    assert_eq(code, 200, "重置应成功")
    new_tok = d.get("token")
    assert_true(new_tok and new_tok != tok, "应换新 token")
    assert_eq(s.get("/api/me", tok)[0], 401, "旧 token 失效")
    assert_eq(s.get("/api/me", new_tok)[0], 200, "新 token 可用")


@case("管理员：用户列表仅管理员可访问")
def t_admin_list(s):
    admin = s.make_user("root", "Root@12345", admin=True)
    normal = s.make_user("dave", "Dave@12345")
    assert_eq(s.get("/api/users", normal)[0], 403, "普通用户应 403")
    code, d = s.get("/api/users", admin)
    assert_eq(code, 200, "管理员应 200")
    names = [u["username"] for u in d.get("users", [])]
    assert_true("root" in names and "dave" in names, "应含所有用户")
    assert_true(all("password_hash" not in u for u in d["users"]), "不得返回密码哈希")


@case("管理员：停用用户后其 token 立即失效且无法登录")
def t_admin_disable(s):
    admin = s.make_user("root2", "Root@12345", admin=True)
    tok = s.make_user("erin", "Erin@12345")
    uid = [u["id"] for u in s.get("/api/users", admin)[1]["users"] if u["username"] == "erin"][0]
    code, _ = s.post("/api/users/active", {"id": uid, "active": False}, admin)
    assert_eq(code, 200, "停用应成功")
    assert_eq(s.get("/api/me", tok)[0], 401, "被停用后 token 应立即失效")
    code, _ = s.post("/api/login", {"username": "erin", "password": "Erin@12345"})
    assert_eq(code, 403, "被停用后应无法登录")


@case("管理员：不能停用自己 / 不能取消自己的管理员")
def t_admin_self_protect(s):
    admin = s.make_user("root3", "Root@12345", admin=True)
    uid = [u["id"] for u in s.get("/api/users", admin)[1]["users"] if u["username"] == "root3"][0]
    assert_eq(s.post("/api/users/active", {"id": uid, "active": False}, admin)[0], 400, "不能停用自己")
    assert_eq(s.post("/api/users/admin", {"id": uid, "admin": False}, admin)[0], 400, "不能降权自己")


@case("管理员：重置他人口令后对方旧 token 失效")
def t_admin_reset_pw(s):
    admin = s.make_user("root4", "Root@12345", admin=True)
    tok = s.make_user("frank", "Frank@12345")
    uid = [u["id"] for u in s.get("/api/users", admin)[1]["users"] if u["username"] == "frank"][0]
    code, _ = s.post("/api/users/password", {"id": uid, "new_password": "Reset@12345"}, admin)
    assert_eq(code, 200, "重置应成功")
    assert_eq(s.get("/api/me", tok)[0], 401, "对方旧 token 应失效")
    assert_eq(s.post("/api/login", {"username": "frank", "password": "Reset@12345"})[0], 200, "新密码可登录")


@case("数据隔离：看不到他人私有记录")
def t_isolation(s):
    a = s.make_user("ua", "Ua@123456")
    b = s.make_user("ub", "Ub@123456")
    code, d = s.post("/api/add", {"name": "A的私有", "price": 1, "category": "其他",
                                  "visibility": "private"}, a)
    assert_eq(code, 200, "新增应成功")
    items_b = s.get("/api/items", b)[1]["items"]
    assert_true(all(it["name"] != "A的私有" for it in items_b), "B 不应看到 A 的私有记录")


@case("可见性：默认 public，可改 private，公开列表随之变化")
def t_visibility(s):
    a = s.make_user("uv", "Uv@123456")
    code, d = s.post("/api/add", {"name": "默认公开商品", "price": 9.9, "category": "其他"}, a)
    iid = d["id"]
    # 默认应为 public
    mine = s.get("/api/items", a)[1]["items"]
    row = [it for it in mine if it["id"] == iid][0]
    assert_eq(row.get("visibility") or "public", "public", "默认可见性")
    # 公开广场能查到
    pub = s.get("/api/public", a)[1]["items"]
    assert_true(any(it["id"] == iid for it in pub), "应出现在公开广场")
    # 改为私有
    code, _ = s.post("/api/update", {"id": iid, "visibility": "private"}, a)
    assert_eq(code, 200, "改为私有应成功")
    pub2 = s.get("/api/public", a)[1]["items"]
    assert_true(all(it["id"] != iid for it in pub2), "私有后不应出现在公开广场")


@case("公开广场：同一商品不同用户各自的评价都可见")
def t_shared_reviews(s):
    a = s.make_user("us1", "Us@123456")
    b = s.make_user("us2", "Us@123456")
    for who, tok, review in (("A", a, "我觉得不错"), ("B", b, "我觉得一般")):
        s.post("/api/add", {"name": "同款饼干", "price": 9.9, "category": "其他",
                            "review": review}, tok)
    pub = s.get("/api/public", a)[1]["items"]
    same = [it for it in pub if it["name"] == "同款饼干"]
    assert_eq(len(same), 2, "两条记录都应在公开广场")
    reviews = sorted(it["review"] for it in same)
    assert_eq(reviews, ["我觉得一般", "我觉得不错"], "各自评价应保留")
    assert_true(all(it.get("owner_name") for it in same), "应带作者名")


@case("公开广场：被停用用户的记录不出现")
def t_public_excludes_disabled(s):
    admin = s.make_user("root5", "Root@12345", admin=True)
    tok = s.make_user("gone", "Gone@12345")
    s.post("/api/add", {"name": "将被隐藏", "price": 1, "category": "其他"}, tok)
    uid = [u["id"] for u in s.get("/api/users", admin)[1]["users"] if u["username"] == "gone"][0]
    s.post("/api/users/active", {"id": uid, "active": False}, admin)
    # flat=1 + 大 size：按原始行查，避免只翻第一页分组带来的偶发漏检
    pub = s.get("/api/public?flat=1&size=100", admin)[1]["items"]
    assert_true(all(it["name"] != "将被隐藏" for it in pub), "停用用户的记录应隐藏")


@case("编辑：不丢品类扩展字段（回归 O-2）")
def t_update_keeps_ext(s):
    tok = s.make_user("ue", "Ue@123456")
    code, d = s.post("/api/add", {
        "name": "测试饼干", "price": 9.9, "category": "食品",
        "ingredients": "小麦粉", "manufacturer": "某厂", "license": "SC123",
        "origin": "山东", "storage_condition": "阴凉", "allergens": "含大豆",
        "flavors": "原味"}, tok)
    iid = d["id"]
    before = [it for it in s.get("/api/items", tok)[1]["items"] if it["id"] == iid][0]
    # 只改名字
    s.post("/api/update", {"id": iid, "name": "改名了"}, tok)
    after = [it for it in s.get("/api/items", tok)[1]["items"] if it["id"] == iid][0]
    for f in ("ingredients", "manufacturer", "license", "origin",
              "storage_condition", "allergens"):
        assert_eq(after.get(f), before.get(f), f"字段 {f} 不应被清空")
    assert_eq(after["name"], "改名了", "名称应已更新")


@case("权限：改他人记录应被拒")
def t_update_others(s):
    a = s.make_user("ua2", "Ua@123456")
    b = s.make_user("ub2", "Ub@123456")
    iid = s.post("/api/add", {"name": "A的商品", "price": 1, "category": "其他"}, a)[1]["id"]
    code, _ = s.post("/api/update", {"id": iid, "name": "被B改了"}, b)
    assert_eq(code, 403, "越权修改应 403")
    code, _ = s.post("/api/delete", {"id": iid}, b)
    assert_eq(code, 403, "越权删除应 403")


@case("删除：级联清理扩展表")
def t_delete_cascade(s):
    tok = s.make_user("ud", "Ud@123456")
    iid = s.post("/api/add", {"name": "待删除", "price": 1, "category": "食品",
                              "ingredients": "x"}, tok)[1]["id"]
    code, _ = s.post("/api/delete", {"id": iid}, tok)
    assert_eq(code, 200, "删除应成功")
    mine = s.get("/api/items", tok)[1]["items"]
    assert_true(all(it["id"] != iid for it in mine), "记录应消失")


@case("购买记录拆表：新增商品自带首条购买记录")
def t_purchase_created_with_item(s):
    tok = s.make_user("ph1", "Ph@123456")
    iid = s.post("/api/add", {"name": "咖啡", "price": 30, "quantity": 2,
                              "purchase_date": "2026-10-01", "channel": "山姆"}, tok)[1]["id"]
    it = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_eq(it["purchase_count"], 1, "新建商品应自带 1 条购买记录")
    assert_eq(it["purchases"][0]["price"], 30.0, "购买记录价格")
    assert_eq(it["purchases"][0]["channel"], "山姆", "购买记录渠道")
    assert_eq(it["purchases"][0]["purchase_date"], "2026-10-01", "购买日期")
    # 库里也应真的落在 purchases 表（不是只在 items 上镜像）
    rows = s.sql("SELECT item_id, price FROM purchases WHERE item_id=?", (iid,))
    assert_eq(len(rows), 1, "purchases 表应有一行")


@case("复购：传 item_id 只新增购买记录，不重复建商品")
def t_repurchase(s):
    tok = s.make_user("ph2", "Ph@123456")
    iid = s.post("/api/add", {"name": "牙膏", "price": 20, "purchase_date": "2026-09-01",
                              "channel": "京东", "category": "美妆护肤"}, tok)[1]["id"]
    code, d = s.post("/api/add", {"item_id": iid, "price": 18, "purchase_date": "2026-10-05"}, tok)
    assert_eq(code, 200, "复购应成功")
    assert_true(d.get("reused"), "应标记为复购")
    items = s.get("/api/items", tok)[1]["items"]
    it = [x for x in items if x["id"] == iid][0]
    assert_eq(len(items), 1, "商品档案不应重复创建")
    assert_eq(it["purchase_count"], 2, "应有 2 条购买记录")
    assert_eq(it["total_spent"], 38.0, "总花费")
    assert_eq(it["avg_price"], 19.0, "均价")
    assert_eq(it["first_purchase_date"], "2026-09-01", "首次购买日期")
    assert_eq(it["last_purchase_date"], "2026-10-05", "最近购买日期")
    # 复购未传渠道 → 沿用上一次（省得重复填）
    assert_eq(it["purchases"][0]["channel"], "京东", "未传字段应沿用上次购买")


@case("复购：不能复购别人的商品")
def t_repurchase_others(s):
    a = s.make_user("ph3a", "Ph@123456")
    b = s.make_user("ph3b", "Ph@123456")
    iid = s.post("/api/add", {"name": "别人的东西", "price": 9}, a)[1]["id"]
    code, _ = s.post("/api/add", {"item_id": iid, "price": 9}, b)
    assert_eq(code, 403, "复购他人商品应 403")


@case("编辑购买字段：同步 purchases，商品档案同步更新")
def t_update_purchase(s):
    tok = s.make_user("ph4", "Ph@123456")
    iid = s.post("/api/add", {"name": "洗发水", "price": 50, "purchase_date": "2026-10-01"}, tok)[1]["id"]
    code, _ = s.post("/api/update", {"id": iid, "price": 45, "channel": "天猫",
                                     "review": "好用"}, tok)
    assert_eq(code, 200, "更新应成功")
    it = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_eq(it["purchases"][0]["price"], 45.0, "购买价格应同步到 purchases")
    assert_eq(it["purchases"][0]["channel"], "天猫", "购买渠道应同步")
    assert_eq(it["review"], "好用", "商品档案字段也应更新")
    assert_eq(it["purchase_count"], 1, "编辑不应新增购买记录")


@case("删除商品：购买记录级联清理")
def t_delete_cascade_purchases(s):
    tok = s.make_user("ph5", "Ph@123456")
    iid = s.post("/api/add", {"name": "纸巾", "price": 10, "purchase_date": "2026-10-01"}, tok)[1]["id"]
    s.post("/api/add", {"item_id": iid, "price": 11, "purchase_date": "2026-10-10"}, tok)
    assert_eq(len(s.sql("SELECT id FROM purchases WHERE item_id=?", (iid,))), 2, "删除前应有 2 条")
    s.post("/api/delete", {"id": iid}, tok)
    assert_eq(len(s.sql("SELECT id FROM purchases WHERE item_id=?", (iid,))), 0, "删除后购买记录应清空")


@case("删除单条购买记录：商品保留，缓存刷新为剩余最近一次")
def t_purchase_delete(s):
    tok = s.make_user("ph6", "Ph@123456")
    iid = s.post("/api/add", {"name": "牛奶", "price": 12, "purchase_date": "2026-10-01",
                              "channel": "盒马"}, tok)[1]["id"]
    s.post("/api/add", {"item_id": iid, "price": 15, "purchase_date": "2026-10-20"}, tok)
    pid = s.sql("SELECT id FROM purchases WHERE item_id=? AND purchase_date=?",
                (iid, "2026-10-20"))[0][0]
    code, d = s.post("/api/purchase/delete", {"id": pid}, tok)
    assert_eq(code, 200, "删除单条购买记录应成功")
    it = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_eq(it["purchase_count"], 1, "应剩 1 条购买记录")
    assert_eq(it["price"], 12.0, "商品上的「最近一次购买」缓存应刷新为剩下的那条")
    assert_eq(it["purchase_date"], "2026-10-01", "缓存日期同步刷新")
    assert_eq(it["channel"], "盒马", "缓存渠道同步刷新")
    # 删别人的购买记录 → 403
    other = s.make_user("ph6b", "Ph@123456")
    code, _ = s.post("/api/purchase/delete", {"id": pid}, other)
    assert_eq(code, 403, "删他人购买记录应 403")
    # 删完最后一条：商品仍在，只是暂时没有购买记录
    pid2 = s.sql("SELECT id FROM purchases WHERE item_id=?", (iid,))[0][0]
    s.post("/api/purchase/delete", {"id": pid2}, tok)
    it = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_eq(it["purchase_count"], 0, "最后一条删掉后次数为 0")
    assert_eq(it["purchases"], [], "购买记录为空数组")


# ---------------------------------------------------------------- 本轮新增
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@case("首次部署：自动建默认管理员，凭 INITIAL_ADMIN.txt 即可登录")
def t_bootstrap_admin(s):
    notice = s.app / "data" / "INITIAL_ADMIN.txt"
    assert_true(notice.exists(), "首次启动应生成 data/INITIAL_ADMIN.txt")
    text = notice.read_text(encoding="utf-8")
    import re as _re
    m = _re.search(r"用户名:\s*(\S+)", text)
    p = _re.search(r"密码:\s*(\S+)", text)
    assert_true(m and p, "文件应含用户名与密码")
    # 密码必须满足强度策略（否则自己生成的密码自己都改不了）
    assert_true(len(p.group(1)) >= 8, "默认密码长度应 >= 8")
    code, d = s.post("/api/login", {"username": m.group(1), "password": p.group(1)})
    assert_eq(code, 200, "默认管理员应能直接登录")
    tok = d.get("token")
    assert_true(bool(tok), "应返回 token")
    code, me = s.get("/api/me", tok)
    assert_true(me.get("is_admin"), "默认账号应是管理员")


@case("首次部署：已有用户时不重复创建管理员（重启幂等）")
def t_bootstrap_idempotent(s):
    assert_eq(s.sql("SELECT COUNT(*) FROM users")[0][0], 1, "空库启动应恰好 1 个用户")
    # 用同一运行目录再跑一次 ensure_schema：应返回 None（不重复建号）
    env = dict(os.environ, SHOPLOG_ROOT=str(s.app))
    r = subprocess.run(
        [PY, "-c", "import server;print(server.ensure_schema())"],
        cwd=str(SCRIPTS), env=env, capture_output=True, text=True, timeout=60)
    assert_eq(r.returncode, 0, f"重跑 ensure_schema 应成功: {r.stderr[-300:]}")
    assert_true("None" in r.stdout, f"已有用户时不应再建管理员，实际输出: {r.stdout.strip()}")
    assert_eq(s.sql("SELECT COUNT(*) FROM users")[0][0], 1, "用户数不应变化")


@case("图片：先上传归档，编辑时可删单张并清理磁盘文件")
def t_images_flow(s):
    tok = s.make_user("img1", "Img@123456")
    code, d = s.post("/api/images", {"images": [PNG_1PX, PNG_1PX],
                                     "purchase_date": "2026-10-01",
                                     "name": "测试图"}, tok)
    assert_eq(code, 200, "预上传应成功")
    paths = d.get("images") or []
    assert_eq(len(paths), 2, "应返回两张图的归档路径")
    for p in paths:
        assert_true(p.startswith("images/"), f"路径应在 images/ 下: {p}")
        assert_true((s.app / p).is_file(), f"磁盘文件应存在: {p}")

    code, d2 = s.post("/api/add", {"name": "带图记录", "price": 1,
                                   "category": "其他", "images": paths}, tok)
    assert_eq(code, 200, "新增应成功")
    iid = d2["id"]
    row = [it for it in s.get("/api/items", tok)[1]["items"] if it["id"] == iid][0]
    assert_eq(len(row["images"]), 2, "记录应关联 2 张图")

    # 编辑：只保留第一张 → 第二张的磁盘文件应被删除
    code, d3 = s.post("/api/update", {"id": iid, "images": [paths[0]]}, tok)
    assert_eq(code, 200, "更新应成功")
    assert_eq(d3.get("images_removed"), 1, "应报告删除 1 个文件")
    assert_true(not (s.app / paths[1]).exists(), "被移除的文件应从磁盘删除")
    assert_true((s.app / paths[0]).is_file(), "保留的文件应还在")
    row2 = [it for it in s.get("/api/items", tok)[1]["items"] if it["id"] == iid][0]
    assert_eq(len(row2["images"]), 1, "记录里应只剩 1 张图")

    # 删单张接口
    code, d4 = s.post("/api/images/delete", {"path": paths[0], "item_id": iid}, tok)
    assert_eq(code, 200, "删单张应成功")
    assert_true(not (s.app / paths[0]).exists(), "文件应已删除")
    row3 = [it for it in s.get("/api/items", tok)[1]["items"] if it["id"] == iid][0]
    assert_eq(len(row3["images"]), 0, "记录里不应再有图")

    # 删除记录时，关联图片也应一起清理
    code, d5 = s.post("/api/images", {"images": [PNG_1PX]}, tok)
    p2 = d5["images"][0]
    iid2 = s.post("/api/add", {"name": "待删带图", "price": 1, "category": "其他",
                               "images": [p2]}, tok)[1]["id"]
    assert_eq(s.post("/api/delete", {"id": iid2}, tok)[0], 200, "删除记录应成功")
    assert_true(not (s.app / p2).exists(), "记录删除后图片文件也应清理")


@case("图片：拒绝越界路径（目录穿越）")
def t_image_traversal(s):
    tok = s.make_user("img2", "Img@123456")
    for bad in ("images/../../etc/passwd", "/etc/passwd", "../data/shopping.db", "data/x.jpg"):
        code, _ = s.post("/api/images/delete", {"path": bad}, tok)
        assert_eq(code, 400, f"越界路径应被拒: {bad}")


@case("公开广场：按商品分组分页，同款不跨页")
def t_public_pagination(s):
    tok = s.make_user("pg1", "Pg@123456")
    for i in range(5):
        s.post("/api/add", {"name": f"分页商品{i}", "price": i + 1, "category": "其他"}, tok)
    for r in ("好用", "一般"):
        s.post("/api/add", {"name": "同款测试品", "price": 9, "category": "其他",
                            "review": r}, tok)

    code, d = s.get("/api/public?page=1&size=3", tok)
    assert_eq(code, 200, "分页请求应成功")
    assert_eq(d["total"], 6, "共 6 件不同商品")
    assert_eq(d["pages"], 2, "size=3 应分 2 页")
    assert_eq(len(d["groups"]), 3, "第一页应有 3 组")
    assert_true(d["has_next"] and not d["has_prev"], "首页的翻页标志应正确")

    d2 = s.get("/api/public?page=2&size=3", tok)[1]
    assert_eq(len(d2["groups"]), 3, "第二页应有 3 组")
    assert_true(d2["has_prev"] and not d2["has_next"], "末页的翻页标志应正确")

    # 同一商品的两条记录必须落在同一组，不能被分页切开
    all_groups = d["groups"] + d2["groups"]
    g = [x for x in all_groups if x["name"] == "同款测试品"]
    assert_eq(len(g), 1, "同款只应出现在一个分组里")
    assert_eq(len(g[0]["items"]), 2, "同款的两条评价应在同一组")

    # 越界页应返回空而不是报错
    d3 = s.get("/api/public?page=99&size=3", tok)[1]
    assert_eq(len(d3["groups"]), 0, "越界页应返回空")
    assert_true(d3["page"] == 99, "页码应被回显")


@case("公开广场：关键词搜索只返回匹配商品")
def t_public_search(s):
    tok = s.make_user("pg2", "Pg@123456")
    s.post("/api/add", {"name": "苹果汁饮料", "price": 5, "category": "其他"}, tok)
    s.post("/api/add", {"name": "香蕉牛奶", "price": 6, "category": "其他"}, tok)
    d = s.get("/api/public?q=" + urllib.parse.quote("苹果"), tok)[1]
    assert_eq(d["total"], 1, "应只命中 1 件商品")
    assert_eq(d["groups"][0]["name"], "苹果汁饮料", "命中商品应正确")


@case("用户：停用 → 回收站 → 恢复 → 永久删除（数据只在最后一步消失）")
def t_user_recycle_bin(s):
    admin = s.make_user("root6", "Root@12345", admin=True)
    tok = s.make_user("bin1", "Bin@123456")
    iid = s.post("/api/add", {"name": "回收站测试商品", "price": 2,
                              "category": "其他"}, tok)[1]["id"]
    uid = s.uid_of(admin, "bin1")

    # 1) 停用：不能登录，但数据仍在
    assert_eq(s.post("/api/users/active", {"id": uid, "active": False}, admin)[0], 200, "停用应成功")
    assert_eq(s.post("/api/login", {"username": "bin1", "password": "Bin@123456"})[0],
              403, "停用后不能登录")
    assert_eq(s.sql("SELECT COUNT(*) FROM items WHERE id=?", (iid,))[0][0], 1, "停用不动数据")

    # 2) 移入回收站：正常列表消失、回收站可见、无法登录、数据保留
    code, _ = s.post("/api/users/delete", {"id": uid}, admin)
    assert_eq(code, 200, "软删应成功")
    names = [u["username"] for u in s.get("/api/users", admin)[1]["users"]]
    assert_true("bin1" not in names, "正常用户列表不应再出现")
    assert_true("bin1" in [u["username"] for u in s.get("/api/users?bin=1", admin)[1]["users"]],
                "应出现在回收站")
    assert_eq(s.post("/api/login", {"username": "bin1", "password": "Bin@123456"})[0],
              401, "回收站账号应按不存在处理")
    assert_eq(s.get("/api/items", tok)[0], 401, "旧 token 应失效")
    assert_eq(s.sql("SELECT COUNT(*) FROM items WHERE id=?", (iid,))[0][0], 1,
              "软删除必须保留数据")
    pub = s.get("/api/public", admin)[1]["items"]
    assert_true(all(it["name"] != "回收站测试商品" for it in pub),
                "回收站账号的记录不应出现在公开广场")

    # 3) 恢复：立即可用
    code, _ = s.post("/api/users/restore", {"id": uid}, admin)
    assert_eq(code, 200, "恢复应成功")
    assert_eq(s.post("/api/login", {"username": "bin1", "password": "Bin@123456"})[0],
              200, "恢复后应能登录")
    assert_true("bin1" in [u["username"] for u in s.get("/api/users", admin)[1]["users"]],
                "应回到正常列表")
    pub2 = s.get("/api/public", admin)[1]["items"]
    assert_true(any(it["name"] == "回收站测试商品" for it in pub2),
                "恢复后记录应重新出现在公开广场")

    # 4) 不在回收站时不能永久删除（防止一步误删）
    assert_eq(s.post("/api/users/purge", {"id": uid}, admin)[0], 400,
              "未进回收站时永久删除应被拒")

    # 5) 软删 → 永久删除：数据真正消失
    assert_eq(s.post("/api/users/delete", {"id": uid}, admin)[0], 200, "再次软删")
    code, d = s.post("/api/users/purge", {"id": uid}, admin)
    assert_eq(code, 200, "永久删除应成功")
    assert_eq(s.sql("SELECT COUNT(*) FROM items WHERE user_id=?", (uid,))[0][0], 0,
              "该用户记录应被删除")
    assert_eq(s.sql("SELECT COUNT(*) FROM users WHERE id=?", (uid,))[0][0], 0,
              "用户应被删除")
    assert_true("bin1" not in [u["username"] for u in s.get("/api/users?bin=1", admin)[1]["users"]],
                "回收站里也应消失")


@case("用户：不能删除自己，也不能永久删除自己")
def t_user_self_delete(s):
    admin = s.make_user("root8", "Root@12345", admin=True)
    uid = s.uid_of(admin, "root8")
    assert_eq(s.post("/api/users/delete", {"id": uid}, admin)[0], 400, "不能删自己")
    assert_eq(s.post("/api/users/purge", {"id": uid}, admin)[0], 400, "不能永久删自己")


@case("用户：重置密码后回收站也登录不了（token 已被轮换）")
def t_user_deleted_token(s):
    admin = s.make_user("root9", "Root@12345", admin=True)
    tok = s.make_user("bin2", "Bin2@1234")
    uid = s.uid_of(admin, "bin2")
    s.post("/api/users/delete", {"id": uid}, admin)
    assert_eq(s.get("/api/me", tok)[0], 401, "进回收站后旧 token 失效")
    # 恢复后旧 token 也回不来（token 已轮换），必须重新登录
    s.post("/api/users/restore", {"id": uid}, admin)
    assert_eq(s.get("/api/me", tok)[0], 401, "恢复不会复活旧 token")


@case("退出登录：服务端下发过期 Cookie（JS 删不掉 HttpOnly）")
def t_logout(s):
    tok = s.make_user("lo1", "Lo@123456")
    code, headers, d = s.req_full("POST", "/api/logout", {}, tok)
    assert_eq(code, 200, "退出应成功")
    cookie = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    assert_true("shopping_token=" in cookie, f"应下发清除 Cookie 的响应头，实际: {cookie!r}")
    assert_true("Max-Age=0" in cookie, f"Cookie 应过期，实际: {cookie!r}")
    assert_true("HttpOnly" in cookie, "清除时也要保持 HttpOnly 属性")
    # 退出不作废 API Token（CLI/skill 仍在用）
    assert_eq(s.get("/api/me", tok)[0], 200, "退出网页不应把 API Token 一起废掉")


@case("设置：本人能看到完整 Token（不脱敏）")
def t_settings_token_plain(s):
    tok = s.make_user("tk1", "Tk@123456")
    code, d = s.get("/api/settings", tok)
    assert_eq(code, 200, "读取设置应成功")
    full = (d.get("user") or {}).get("token") or ""
    assert_eq(full, tok, "应返回完整 Token 供复制")
    assert_true("*" not in full, "不应含掩码星号")


@case("公开广场：不泄露购买记录（价格/日期/渠道/地点/数量）")
def t_public_no_purchase_data(s):
    tok = s.make_user("pv1", "Pv@123456")
    s.post("/api/add", {
        "name": "公开字段测试品", "price": 88.8, "category": "食品",
        "purchase_date": "2026-10-01", "channel": "天猫", "place": "上海",
        "quantity": 3, "review": "还不错", "share_note": "推荐",
        "images": ["images/2026-10/fake.jpg"], "rating": 4,
        "ingredients": "小麦粉", "brand": "某牌", "tags": "零食",
    }, tok)
    d = s.get("/api/public", tok)[1]
    g = [x for x in d["groups"] if x["name"] == "公开字段测试品"][0]
    it = g["items"][0]

    # 0.5.2 起公开广场是大图卡片：图片 / 成分 / 营养 / 食用方式改为可公开；
    # 购买记录类字段（含生产日期、到期日）仍然一律不出现。
    for hidden in ("price", "purchase_date", "channel", "place", "quantity",
                   "currency", "paid_price", "original_price",
                   "unit_price", "purchase_history", "source",
                   "expiry_date", "produced_date"):
        assert_true(hidden not in it, f"购买/明细字段 {hidden} 不应出现在公开接口")

    for shown in ("name", "brand", "category", "review", "share_note", "rating",
                  "owner_name", "images", "ingredients", "eating_method"):
        assert_true(shown in it, f"可共享字段 {shown} 应出现")

    # 分组里也不应带「最近购买时间」
    assert_true("last_date" not in g, "分组不应返回 last_date（购买时间）")


@case("公开图片：只流出挂在公开记录上的图（私有/越界一律拒绝）")
def t_public_image_guard(s):
    tok = s.make_user("pi1", "Pi@123456")
    # 公开记录带一张图
    s.post("/api/add", {
        "name": "公开图片测试品", "category": "食品", "purchase_date": "2026-10-02",
        "images": ["images/2026-10/pub-visible.jpg"], "visibility": "public",
    }, tok)
    # 私有记录带一张图
    s.post("/api/add", {
        "name": "私有图片测试品", "category": "食品", "purchase_date": "2026-10-02",
        "images": ["images/2026-10/pub-hidden.jpg"], "visibility": "private",
    }, tok)

    ok_pub = s.raw_get("/api/public-image?p=images/2026-10/pub-visible.jpg")
    ok_priv = s.raw_get("/api/public-image?p=images/2026-10/pub-hidden.jpg")
    ok_none = s.raw_get("/api/public-image?p=images/2026-10/nope.jpg")
    ok_esc = s.raw_get("/api/public-image?p=" + urllib.parse.quote("../config.example.json"))

    # 文件本身不存在（用例不落盘真实图片）→ 404 而不是 200 裸奔；
    # 关键是「私有图 / 越界路径」绝不能返回 200
    assert_true(ok_priv[0] in (403, 404), f"私有记录图片不应可访问，实际 {ok_priv[0]}")
    assert_true(ok_none[0] == 404, f"不存在的图片应 404，实际 {ok_none[0]}")
    assert_true(ok_esc[0] in (400, 403, 404), f"越界路径应被拒绝，实际 {ok_esc[0]}")
    assert_true(ok_pub[0] in (200, 404), f"公开图片接口应正常响应，实际 {ok_pub[0]}")


@case("公开广场：免登录可访问")
def t_public_no_auth(s):
    s.make_user("pv2", "Pv@123456")
    code, d = s.get("/api/public")   # 不带任何 token
    assert_eq(code, 200, "未登录也应能读公开广场")
    assert_true(isinstance(d.get("groups"), list), "应返回分组数组")


# ------------------------------------------------- 前端展示层（shared.js 单一来源）
# 背景：个人页与公开广场曾经各写一份展示逻辑，导致公开页星级用 Math.round 取整、
# 半星显示不出来，营养成分单位也不一致（千焦/克 vs kJ/g）。
# 现在两页必须共用 site/shared.js，任何人再把公共函数抄回页面里都会被这里拦下。
SHARED_NAMES = ["esc", "money", "amt", "starStr", "healthScore", "healthGrade",
                "healthColor", "nutriTable", "nutriSummary", "initial"]
CONST_NAMES = ["NUTRI_LABEL", "NUTRI_UNIT", "NRV", "NUTRI_ORDER"]
REDEFINE_RE = re.compile(
    r"^\s*(?:function\s+(?:%s)\b|const\s+(?:%s)\b)" % ("|".join(SHARED_NAMES), "|".join(CONST_NAMES)),
    re.M)


@case("展示层：两页共用 shared.js，公共函数不得重复定义")
def t_shared_display_layer(s):
    site = ROOT / "site"
    shared = site / "shared.js"
    assert_true(shared.exists(), "缺少 site/shared.js")

    shared_txt = shared.read_text(encoding="utf-8")
    for fn in SHARED_NAMES:
        assert_true(re.search(r"function\s+%s\s*\(" % fn, shared_txt),
                    f"shared.js 缺少函数 {fn}")

    for page in ("index.html", "public.html"):
        p = site / page
        assert_true(p.exists(), f"缺少 site/{page}")
        txt = p.read_text(encoding="utf-8")
        assert_true('src="/shared.js"' in txt, f"site/{page} 未引用 /shared.js")
        bad = REDEFINE_RE.findall(txt)
        assert_true(not bad, f"site/{page} 重复定义了公共函数 {bad}，展示逻辑必须只写在 shared.js")

    # nginx 必须放行 /shared.js，否则免登录的公开广场加载不到
    conf = ROOT / "docs" / "nginx.shopping.conf.example"
    if conf.exists():
        c = conf.read_text(encoding="utf-8")
        assert_true("location = /shared.js" in c,
                    "nginx 模板未放行 /shared.js，公开广场会白屏")


@case("展示层：星级支持半星（node 实跑 shared.js）")
def t_shared_star_half(s):
    shared_txt = (ROOT / "site" / "shared.js").read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        # 没有 node 时退化为源码断言：至少不能再用 Math.round 取整星级
        body = shared_txt.split("function starStr")[1][:400]
        assert_true("Math.round" not in body, "starStr 不应再用 Math.round 取整（半星会丢）")
        assert_true('class="half"' in shared_txt, "starStr 缺少半星实现")
        return

    js = """
    global.window = global;
    global.document = {
      getElementById: function(){ return null; },
      head: { appendChild: function(){} },
      createElement: function(){ return {}; }
    };
    """ + shared_txt + """
    console.log(JSON.stringify({
      s35: SL.starStr(3.5), s4: SL.starStr(4), s0: SL.starStr(0), s5: SL.starStr(5),
      esc: SL.esc('<a href="x">'), money: SL.money(1234.5),
      hcNull: SL.healthColor(null), hc85: SL.healthColor(85),
      nutri: SL.nutriTable({energy_kj:840, protein_g:6}),
      grade: SL.healthGrade(85)
    }));
    """
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=60)
    assert_eq(r.returncode, 0, f"shared.js 在 node 下执行失败：{r.stderr[:300]}")
    d = json.loads(r.stdout.strip().splitlines()[-1])

    # 3.5 星 = 3 颗实心 + 1 颗半星 + 1 颗空心
    assert_eq(d["s35"].count('class="on"'), 3, "3.5 星应有 3 颗实心")
    assert_eq(d["s35"].count('class="half"'), 1, "3.5 星应有 1 颗半星")
    assert_eq(d["s35"].count("<i>★</i>"), 1, "3.5 星应有 1 颗空心")
    assert_eq(d["s4"].count('class="on"'), 4, "4 星应有 4 颗实心")
    assert_eq(d["s4"].count('class="half"'), 0, "整数分不应有半星")
    assert_eq(d["s5"].count('class="on"'), 5, "5 星应全实心")
    assert_eq(d["s0"], "", "0 分不渲染星级")
    assert_true("&lt;" in d["esc"] and "&quot;" in d["esc"], "esc 转义不完整")
    assert_eq(d["money"], "¥1,234.50", "金额格式应为 ¥1,234.50")
    assert_eq(d["hcNull"], "", "健康分为 null 不应上色")
    assert_true(d["hc85"], "健康分 85 应返回颜色")
    assert_eq(d["grade"], "A", "85 分应为 A 级")
    assert_true("千焦" in d["nutri"], "营养单位应统一为中文（千焦/克/毫克）")


# ---------------------------------------------------------------- 健康评分
# 评分的唯一权威是 scripts/health_score.py：新增 / 编辑都必须在服务端算好，
# 前端只展示不再另算一套（否则同一件商品两个页面两个分）。
BAD_NUTRITION = {"energy_kj": 2500, "protein_g": 2, "fat_g": 40,
                 "carbs_g": 60, "sodium_mg": 900}
BAD_INGREDIENTS = "棕榈油、白砂糖、食用香精、氢化植物油"
GOOD_NUTRITION = {"energy_kj": 300, "protein_g": 10, "fat_g": 1,
                  "carbs_g": 10, "fiber_g": 6, "sodium_mg": 50}
GOOD_INGREDIENTS = "燕麦、全麦粉"


@case("健康评分：新增记录服务端自动算分并落库")
def t_health_on_add(s):
    tok = s.make_user("hs1", "Hs@123456")
    code, d = s.post("/api/add", {
        "name": "高油薯片", "price": 6.9, "category": "食品",
        "nutrition": BAD_NUTRITION, "ingredients": BAD_INGREDIENTS,
    }, tok)
    assert_eq(code, 200, "新增应成功")
    iid = d["id"]
    it = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    hs = health_score.score_food(BAD_NUTRITION, BAD_INGREDIENTS)
    assert_true(it.get("health_score") is not None, "新增应自动算健康分（不能是 NULL）")
    assert_eq(it["health_score"], hs["score"], "落库分数应与后端规则一致")
    assert_eq(it["health_level"], hs["level"], "档位名称应与后端规则一致")
    assert_eq(it["health_grade"], hs["grade"], "A/B/C/D 档次应由后端换算")
    assert_eq(it["health_grade"], "D", "高油高钠零食应是 D 档")
    # 库里也要真的写进去（不是只在接口返回时现算）
    rows = s.sql("SELECT health_score, health_level FROM items WHERE id=?", (iid,))
    assert_eq(rows[0][0], hs["score"], "items.health_score 应落库")
    assert_eq(rows[0][1], hs["level"], "items.health_level 应落库")


@case("健康评分：没有营养也没有配料时不硬凑分")
def t_health_skips_without_data(s):
    tok = s.make_user("hs0", "Hs@123456")
    iid = s.post("/api/add", {"name": "纯棉T恤", "price": 59, "category": "衣服"}, tok)[1]["id"]
    it = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_eq(it.get("health_score"), None, "无营养/配料数据不应给分")
    assert_eq(it.get("health_grade"), None, "无数据不应有档次")


@case("健康评分：改营养或配料后自动重算")
def t_health_on_update(s):
    tok = s.make_user("hs2", "Hs@123456")
    iid = s.post("/api/add", {
        "name": "燕麦片", "price": 19.9, "category": "食品",
        "nutrition": GOOD_NUTRITION, "ingredients": GOOD_INGREDIENTS,
    }, tok)[1]["id"]
    before = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_eq(before["health_grade"], "A", "高纤低脂应是 A 档")
    # 只改配料（营养还是好的）→ 也要重算，且不能因为没传 nutrition 就把营养判空
    s.post("/api/update", {"id": iid, "ingredients": BAD_INGREDIENTS}, tok)
    mid = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_true(mid["health_score"] < before["health_score"],
                f"配料变差后分数应下降（{before['health_score']} → {mid['health_score']}）")
    # 再改营养 → 继续重算
    s.post("/api/update", {"id": iid, "nutrition": BAD_NUTRITION}, tok)
    after = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_true(after["health_score"] < mid["health_score"], "营养变差后分数应继续下降")
    assert_eq(after["health_grade"], "D", "高油高钠应是 D 档")
    # 人工复核过的分（显式传 health_score）不被覆盖
    s.post("/api/update", {"id": iid, "health_score": 88}, tok)
    manual = [x for x in s.get("/api/items", tok)[1]["items"] if x["id"] == iid][0]
    assert_eq(manual["health_score"], 88.0, "显式传的分应保留（人工复核优先）")


@case("公开广场：按健康档次 A/B/C/D 筛选")
def t_public_grade_filter(s):
    tok = s.make_user("hs3", "Hs@123456")
    s.post("/api/add", {"name": "燕麦能量棒", "price": 9.9, "category": "食品",
                        "nutrition": GOOD_NUTRITION, "ingredients": GOOD_INGREDIENTS}, tok)
    s.post("/api/add", {"name": "罪恶薯片", "price": 6.9, "category": "食品",
                        "nutrition": BAD_NUTRITION, "ingredients": BAD_INGREDIENTS}, tok)
    s.post("/api/add", {"name": "纯棉卫衣", "price": 199, "category": "衣服"}, tok)

    def names(grade):
        d = s.get(f"/api/public?flat=1&size=100&grade={grade}", tok)[1]
        assert_true(d.get("ok"), f"grade={grade} 查询失败")
        return [it["name"] for it in d["items"]]

    assert_eq(sorted(names("A")), ["燕麦能量棒"], "A 档只应有高分商品")
    assert_eq(sorted(names("D")), ["罪恶薯片"], "D 档只应有低分商品")
    assert_eq(sorted(names("B")) + sorted(names("C")), [], "本例没有 B/C 档商品")
    assert_eq(sorted(names("none")), ["纯棉卫衣"], "none = 还没评分的商品")
    code, _ = s.get("/api/public?grade=X", tok)
    assert_eq(code, 400, "未知档次应 400")


@case("健康档次：后端 GRADE_BANDS 与前端 shared.js 阈值一致")
def t_grade_bands_consistent(s):
    # 后端阈值
    expect = {80: "A", 79: "B", 60: "B", 59: "C", 40: "C", 39: "D", 0: "D"}
    for score, grade in expect.items():
        g, name = health_score.grade_of(score)
        assert_eq(g, grade, f"{score} 分的档次应为 {grade}")
        assert_true(name, f"{score} 分应有档位名称")
    assert_eq(health_score.grade_of(None)[0], None, "空分不应有档次")

    # 前端阈值（node 实跑 shared.js 的 healthGrade，逐档比对）
    node = shutil.which("node")
    if not node:
        return
    shared_txt = (ROOT / "site" / "shared.js").read_text(encoding="utf-8")
    js = """
    global.window = global;
    global.document = {getElementById: function(){ return null; },
                       head: {appendChild: function(){}}, createElement: function(){ return {}; }};
    """ + shared_txt + """
    var scores = [0, 39, 40, 59, 60, 79, 80, 100];
    console.log(JSON.stringify(scores.map(function(s){ return [s, SL.healthGrade(s)]; })));
    """
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=60)
    assert_eq(r.returncode, 0, f"shared.js 执行失败：{r.stderr[:300]}")
    for score, grade in json.loads(r.stdout.strip().splitlines()[-1]):
        assert_eq(grade, health_score.grade_of(score)[0],
                  f"前端与后端在 {score} 分的档次不一致（前端 {grade}）")


# ---------------------------------------------------------------- 运行
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--filter", default=None, help="只跑名称含此串的用例")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    selected = [(n, f) for n, f in CASES if not args.filter or args.filter in n]
    if not selected:
        print(f"没有匹配的用例（filter={args.filter}）")
        return 1

    print(f"ShoppingLog 测试：共 {len(selected)} 个用例\n")
    passed, failed = 0, []
    t0 = time.time()
    for name, fn in selected:
        try:
            with Server() as s:
                fn(s)
            passed += 1
            print(f"  ✓ {name}")
        except Exception as e:
            failed.append((name, e, traceback.format_exc()))
            print(f"  ✗ {name}")
            print(f"      {type(e).__name__}: {e}")
            if args.verbose:
                print(traceback.format_exc())

    dt = time.time() - t0
    print(f"\n{'='*60}")
    print(f"结果：{passed} 通过 / {len(failed)} 失败 / 共 {len(selected)}  用时 {dt:.1f}s")
    if failed:
        print("\n失败详情：")
        for name, e, tb in failed:
            print(f"\n--- {name} ---\n{tb}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
