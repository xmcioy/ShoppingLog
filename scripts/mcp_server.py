#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShoppingLog MCP —— stdio 传输管道（薄壳）。

为什么还存在这个文件
--------------------
MCP 的 stdio 传输要求**客户端拥有进程的 stdin/stdout**。而网页服务是常驻守护进程，
stdout 已经重定向到日志文件，没法再当 stdio 通道。所以只支持 stdio 的客户端
（如某些桌面版）必须由它自己拉起一个子进程。

**但这个子进程不含任何业务逻辑**：它只把读到的 JSON-RPC 原样转发给服务进程的
`POST /api/mcp`，再把响应打回 stdout。所有工具实现、鉴权、数据校验都在
`scripts/server.py`（同一个服务）里，与网页走完全相同的链路。

也就是说：MCP 是服务的一个能力，不是第二个服务。

如果客户端支持 HTTP 传输，可以完全不要这个文件，直接配：
  url:     http://127.0.0.1:8088/api/mcp
  headers: {"Authorization": "Bearer <网页设置里的 API Token>"}

环境变量
--------
  SHOPLOG_API_URL    服务地址，默认 http://127.0.0.1:8088
  SHOPLOG_API_TOKEN  用户 API Token（网页「设置」里复制），**必填**

自检：
  SHOPLOG_API_TOKEN=xxx python3 scripts/mcp_server.py --selftest
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = (os.environ.get("SHOPLOG_API_URL") or "http://127.0.0.1:8088").rstrip("/")
API_TOKEN = os.environ.get("SHOPLOG_API_TOKEN") or ""
MCP_PATH = "/api/mcp"


def post(payload, timeout=180):
    """把一条 JSON-RPC 消息转发给服务进程。返回 (status, body_text)。"""
    if not API_TOKEN:
        raise RuntimeError(
            "缺少 SHOPLOG_API_TOKEN。请在 MCP 配置里填网页「设置 → API Token」的值。"
        )
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API_URL + MCP_PATH, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {API_TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error") or body
        except Exception:
            detail = body
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"连不上 {API_URL}：{e.reason}。确认 ShoppingLog 服务已启动"
            f"（bash scripts/deploy.sh 或 install-service.sh）。"
        )


def serve():
    """逐行读 stdin，转发，逐行写 stdout。stdout 只放 JSON，日志一律走 stderr。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": f"JSON 解析失败: {e}"}},
                             ensure_ascii=False), flush=True)
            continue
        try:
            status, text = post(req)
        except Exception as e:
            # 整条请求失败：只对"有 id"的消息回错误（通知无需响应）
            if isinstance(req, dict) and req.get("id") is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                  "error": {"code": -32603, "message": str(e)}},
                                 ensure_ascii=False), flush=True)
            continue
        if status == 202 or not text.strip():
            continue          # 通知：不回包
        print(text.strip(), flush=True)


def selftest():
    print(f"API_URL = {API_URL}")
    print(f"TOKEN   = {'已配置(' + str(len(API_TOKEN)) + ' 位)' if API_TOKEN else '未配置'}")
    probes = [
        ("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "selftest", "version": "1"}}),
        ("tools/list", {}),
        ("tools/call", {"name": "whoami", "arguments": {}}),
        ("tools/call", {"name": "stats", "arguments": {}}),
    ]
    for i, (method, params) in enumerate(probes, 1):
        try:
            _, text = post({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
            d = json.loads(text)
            if "error" in d:
                print(f"[{method}] 失败: {d['error']}")
            elif method == "tools/call":
                print(f"[{method}] {d['result']['content'][0]['text'][:150]}")
            elif method == "tools/list":
                print(f"[{method}] 工具数 {len(d['result']['tools'])}: "
                      f"{', '.join(t['name'] for t in d['result']['tools'])}")
            else:
                print(f"[{method}] {d['result'].get('serverInfo')}")
        except Exception as e:
            print(f"[{method}] 失败: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="本地自检，不进入 stdio 循环")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    serve()


if __name__ == "__main__":
    main()
