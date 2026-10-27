#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 GitHub Git Database API 推送清理后的历史（不依赖 git 协议直连）。

背景：本机到 github.com 的 git 协议（HTTPS/SSH push）不可达，
但 api.github.com 走代理可用，且 token 具备 repo scope。
因此改用 API 逐个上传 blob → tree → commit，最后更新 refs/heads/main。

用法：
  python3 scripts/push_via_api.py --token <TOKEN> --repo xmcioy/ShoppingLog
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


def api(method, path, token, payload=None, api_base="https://api.github.com"):
    """调用 GitHub API。payload 必须是 dict 或 None。"""
    url = api_base + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "ShoppingLog-push-via-api")
    if data:
        req.add_header("Content-Type", "application/json")
    # 走本机代理（api.github.com 在直连下不可达）
    if PROXY:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": PROXY, "https": PROXY}))
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"[api] {method} {path} → HTTP {e.code}\n{body}")


def git(*args):
    return subprocess.run(["git", *args], cwd=str(ROOT),
                          capture_output=True, text=True, check=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--source", default="main", help="本地要推送的 ref")
    args = ap.parse_args()

    api_base = "https://api.github.com"
    # 1) 远端当前 ref（lease 锚点）
    ref_path = f"/repos/{args.repo}/git/ref/heads/{args.branch}"
    cur = api("GET", ref_path, args.token, api_base=api_base)
    old_sha = cur.get("object", {}).get("sha")
    if not old_sha:
        raise SystemExit(f"[api] 取不到远端 {args.branch} 的当前 SHA")
    print(f"[1/6] 远端 {args.branch} 当前: {old_sha[:12]}")

    # 2) 收集本地要推送的提交（从远端旧 SHA 对应的位置开始的新历史）
    #    本地 main 是清理后的历史，与远端无共同祖先 → 整条线都要传
    commits = git("rev-list", "--reverse", args.source).splitlines()
    print(f"[2/6] 待上传提交数: {len(commits)}")

    # 3) 上传所有 blob（先建 tree 需要 blob）
    tree_map = {}   # commit_sha -> tree_sha
    upload_map = {} # 本地 commit sha -> GitHub commit sha
    blob_cache = {}
    uploaded = 0

    def upload_blob(data: bytes) -> str:
        nonlocal uploaded
        key = __import__("hashlib").sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if key in blob_cache:
            return key
        sha = api("POST", f"/repos/{args.repo}/git/blobs", args.token,
                  {"content": base64.b64encode(data).decode(), "encoding": "base64"},
                  api_base=api_base)["sha"]
        blob_cache[key] = sha
        uploaded += 1
        return sha

    for i, c in enumerate(commits, 1):
        # 该提交相对其父提交的变更
        parents = git("rev-list", "--parents", "-n", "1", c).split()[1:]
        parent_tree = tree_map.get(parents[0]) if parents else None
        if parent_tree is None and parents:
            parent_tree = git("rev-parse", f"{parents[0]}^{{tree}}")

        # 收集该提交的所有文件 blob
        entries = []
        files = git("ls-tree", "-r", "--name-only", c).splitlines()
        for f in files:
            mode, typ, sha = git("ls-tree", c, "--", f).split("\t")[0].split()
            if typ != "blob":
                continue
            blob_sha = upload_blob(subprocess.run(
                ["git", "cat-file", "blob", sha], cwd=str(ROOT),
                capture_output=True, check=True).stdout)
            entries.append({"path": f, "mode": mode, "type": "blob", "sha": blob_sha})
        if not entries:
            continue

        tree_payload = {"tree": entries}
        if parent_tree:
            tree_payload["base_tree"] = parent_tree
        tree_sha = api("POST", f"/repos/{args.repo}/git/trees", args.token,
                       tree_payload, api_base=api_base)["sha"]

        msg = git("log", "-1", "--format=%B", c).strip()
        author = git("log", "-1", "--format=%an", c)
        email = git("log", "-1", "--format=%ae", c)
        date = git("log", "-1", "--format=%aI", c)

        # parents 必须是 **GitHub 上已存在** 的 commit SHA。
        # 本地 commit 的 SHA 与 GitHub 上新建 commit 的 SHA 不同
        # （GitHub 会按自己的格式重算），所以必须用 upload_map 做映射。
        # commits 是 --reverse 的拓扑序，父提交一定先于子提交处理完毕。
        remote_parents = []
        for p in parents:
            rp = upload_map.get(p)
            if rp is None:
                raise SystemExit(f"[api] 父提交 {p[:12]} 尚未上传，无法建立父子关系")
            remote_parents.append(rp)

        cmt_payload = {
            "message": msg,
            "tree": tree_sha,
            "parents": remote_parents,
            "author": {"name": author, "email": email, "date": date},
            "committer": {"name": author, "email": email, "date": date},
        }
        new_sha = api("POST", f"/repos/{args.repo}/git/commits", args.token,
                      cmt_payload, api_base=api_base)["sha"]
        upload_map[c] = new_sha
        print(f"      [{i}/{len(commits)}] {c[:8]} → {new_sha[:8]}  ({len(entries)} 文件)")

    head = commits[-1]
    head_sha = upload_map[head]
    print(f"[3/6] blob 上传 {uploaded} 个，HEAD commit = {head_sha[:12]}")

    # 4) 更新 ref（强制，指向新历史）
    # 注意：GitHub 要求完整 40 位 SHA，缩写会返回 422/404
    upd = api("PATCH", ref_path, args.token,
              {"sha": head_sha, "force": True}, api_base=api_base)
    print(f"[4/6] refs/heads/{args.branch} 已更新: {old_sha[:12]} → {upd.get('object', {}).get('sha', head_sha)[:12]}")

    # 5) 校验
    after = api("GET", ref_path, args.token, api_base=api_base)
    print(f"[5/6] 远端现值: {after.get('object', {}).get('sha', '')[:12]}")

    # 6) 清理悬空引用（可选，失败不影响）
    print("[6/6] 完成")


if __name__ == "__main__":
    main()
