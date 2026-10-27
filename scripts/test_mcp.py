#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 服务端到端测试。

在**临时目录**起一个真实 API 服务（SQLite，不碰生产数据），然后用 stdio
JSON-RPC 协议驱动 scripts/mcp_server.py，验证：

  1. initialize / tools/list 握手正常
  2. upload_images 能把本地图片归档并返回路径
  3. add_item 能写入（含扩展字段、标签数组、可见性）
  4. get_item / list_items / stats 读回一致
  5. update_item 只改传入字段，扩展字段与图片不丢
  6. private 记录不出现在 public_feed
  7. delete_item 之后读不到

用法：
  python3 scripts/test_mcp.py
  python3 scripts/test_mcp.py -v
"""

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class McpClient:
    def __init__(self, env):
        self.p = subprocess.Popen(
            [PY, "scripts/mcp_server.py"], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1)
        self._id = 0

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        line = self.p.stdout.readline()
        if not line:
            err = self.p.stderr.read() if self.p.stderr else ""
            raise RuntimeError(f"MCP 进程无响应（可能启动失败）:\n{err[:500]}")
        return json.loads(line)

    def call(self, name, args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = res["content"][0]["text"]
        if res.get("isError"):
            raise AssertionError(f"{name} 失败: {text}")
        return json.loads(text)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sl_mcp_test_"))
    app = tmp / "app"
    app.mkdir(parents=True)
    port = free_port()
    srv = None
    cli = None
    try:
        # ---- 起一个真实服务（SQLite，独立目录） ----
        env_srv = dict(os.environ, SHOPLOG_ROOT=str(app))
        srv = subprocess.Popen(
            [PY, "server.py", "--port", str(port)], cwd=str(SCRIPTS), env=env_srv,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1).read()
                break
            except Exception:
                if srv.poll() is not None:
                    raise RuntimeError("API 服务启动失败")
                time.sleep(0.1)
        else:
            raise RuntimeError("API 服务启动超时")

        # ---- 登录拿 token（这就是网页里那个 API Token） ----
        notice = (app / "data" / "INITIAL_ADMIN.txt").read_text(encoding="utf-8")
        import re
        user = re.search(r"用户名:\s*(\S+)", notice).group(1)
        pw = re.search(r"密码:\s*(\S+)", notice).group(1)
        req = urllib.request.Request(
            base + "/api/login",
            data=json.dumps({"username": user, "password": pw}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            token = json.loads(r.read())["token"]

        # ---- 起 MCP 服务 ----
        env_mcp = dict(os.environ, SHOPLOG_API_URL=base, SHOPLOG_API_TOKEN=token)
        cli = McpClient(env_mcp)

        print("【1】握手与工具清单")
        init = cli.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}})
        check("initialize 返回 serverInfo", init["result"]["serverInfo"]["name"] == "shoppinglog")
        cli.rpc("notifications/initialized", notify=True)
        tools = [t["name"] for t in cli.rpc("tools/list")["result"]["tools"]]
        check("9 个工具齐全", len(tools) == 9, str(tools))

        print("【2】鉴权：whoami 能识别账号")
        me = cli.call("whoami", {})
        check("whoami 返回用户名", me["username"] == user, str(me))

        print("【3】缺参数应报错而不是静默写入")
        res = cli.rpc("tools/call", {"name": "add_item", "arguments": {"name": "缺价格"}})["result"]
        check("缺 price 报错", res["isError"] is True)

        print("【4】upload_images 归档图片")
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        img = tmp / "mcp_test.png"
        img.write_bytes(png)
        up = cli.call("upload_images", {"paths": [str(img)], "name": "MCP测试",
                                        "purchase_date": "2026-10-24"})
        check("返回归档路径", bool(up["images"]) and up["images"][0].startswith("images/"),
              str(up))
        check("文件真的落盘", (app / up["images"][0]).is_file())

        print("【5】add_item 写入（含扩展字段）")
        added = cli.call("add_item", {
            "name": "MCP 测试饼干", "price": 9.9, "category": "食品",
            "purchase_date": "2026-10-24", "brand": "测试牌", "channel": "天猫",
            "rating": 4, "review": "通过 MCP 写入", "tags": ["零食", "测试"],
            "visibility": "private", "images": up["images"],
            "item": {"ingredients": "小麦粉", "shelf_life": "180天", "license": "SC123"},
        })
        iid = added["id"]
        check("返回新 id", isinstance(iid, int) and iid > 0)

        print("【6】get_item 读回一致")
        it = cli.call("get_item", {"id": iid})
        check("名称", it["name"] == "MCP 测试饼干")
        check("价格", float(it["price"]) == 9.9)
        check("标签数组", it["tags"] == ["零食", "测试"], str(it.get("tags")))
        check("扩展字段 ingredients", it.get("ingredients") == "小麦粉")
        check("可见性 private", it.get("visibility") == "private")
        check("图片挂上了", (it.get("images") or []) == up["images"])

        print("【7】update_item 只改传入字段")
        cli.call("update_item", {"id": iid, "item": {"review": "改过了", "rating": 5}})
        it2 = cli.call("get_item", {"id": iid})
        check("review 已更新", it2["review"] == "改过了")
        check("rating 已更新", float(it2["rating"]) == 5)
        check("扩展字段未被清空", it2.get("ingredients") == "小麦粉")
        check("图片未被清空", len(it2.get("images") or []) == len(up["images"]))

        print("【8】list_items / stats")
        r = cli.call("list_items", {"q": "MCP", "limit": 5})
        check("关键词命中", r["total_matched"] == 1, str(r["total_matched"]))
        r2 = cli.call("list_items", {"category": "食品"})
        check("分类命中", r2["total_matched"] == 1)
        st = cli.call("stats", {})
        check("统计条数", st["records"] == 1, str(st))

        print("【9】public_feed 不含 private")
        pf = cli.call("public_feed", {"size": 5})
        names = [g["name"] for g in pf["groups"]]
        check("私有记录不出现在公开广场", "MCP 测试饼干" not in names, str(names))

        print("【10】delete_item")
        cli.call("delete_item", {"id": iid})
        try:
            cli.call("get_item", {"id": iid})
            check("删除后读不到", False)
        except AssertionError:
            check("删除后读不到", True)

        print("【11】HTTP 传输 /api/mcp（MCP 内置在服务进程里）")
        def http_mcp(payload, token=token):
            req = urllib.request.Request(
                base + "/api/mcp", data=json.dumps(payload).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        c, d = http_mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2024-11-05",
                                    "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        check("initialize 走 HTTP", c == 200 and d["result"]["serverInfo"]["name"] == "shoppinglog")

        c, d = http_mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("通知返回 202", c == 202, str(c))

        c, d = http_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        check("HTTP tools/list 9 个", len(d["result"]["tools"]) == 9)

        c, d = http_mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, token="")
        check("HTTP 无 token 应 401", c == 401, str(c))

        c, d = http_mcp([{"jsonrpc": "2.0", "id": 4, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
        check("批量消息返回 2 条", c == 200 and len(d) == 2, str(d)[:80])

        print("\n" + "=" * 56)
        if FAILS:
            print(f"MCP 测试：{len(FAILS)} 项失败 → {FAILS}")
            return 1
        print("MCP 测试：全部通过 ✓")
        return 0
    finally:
        if cli:
            cli.close()
        if srv and srv.poll() is None:
            srv.terminate()
            try:
                srv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
