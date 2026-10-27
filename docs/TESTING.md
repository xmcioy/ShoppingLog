# 测试与验收

## 1. 两套测试，commit 前必须全绿

| 命令 | 覆盖 | 说明 |
|---|---|---|
| `python3 scripts/run_tests.py` | 集成测试（47 项） | 每个用例在**临时运行目录**起一个真实 HTTP 服务（随机端口），互不干扰；只依赖标准库 |
| `python3 scripts/test_mcp.py` | MCP 协议（stdio + HTTP） | 临时库起真实服务，21 项断言 + 5 项 HTTP 传输用例 |

常用参数：
```
python3 scripts/run_tests.py            # 全量
python3 scripts/run_tests.py -k auth    # 只跑名称含 auth 的用例
python3 scripts/run_tests.py -v         # 显示每个用例
```

### 集成测试覆盖了什么

鉴权与账号：密码策略、改密、Token 重置、管理员权限与自保护、退出登录、用户生命周期
（停用 → 回收站 → 恢复 → 永久删除）、数据隔离。

数据与记录：新增 / 编辑 / 删除、扩展字段不丢失、品类迁移、图片归档与单张删除、
目录穿越防护、可见性与公开广场（不泄露购买记录、只流出公开图片）。

**购买记录（0.6.0）**：新建商品自带首条购买记录；复购只新增 `purchases` 不重复建商品；
不能复购他人商品；编辑购买字段同步 `purchases`；删除商品级联清理购买记录。

版本号：`/api/version` 与仓库 `VERSION` 一致且免鉴权；MCP `initialize.serverInfo.version` 一致、
无效 Token 返回 401 JSON。

## 2. 部署验收（改完代码必须做，不允许发布未验证的代码）

```bash
python3 scripts/run_tests.py       # 全绿
bash scripts/deploy.sh             # 部署并自检
```

`deploy.sh` 会自动做这些断言，任一不过就 `DEPLOY_FAIL`：

1. `http://127.0.0.1:8787/api/health` 存活；
2. 仓库 `VERSION` == 线上 `/api/version`（不一致 = 没部署上去）；
3. **8088 复核**：前端可访问 + `/api/version` 一致 + `/api/mcp` 可达
   （带 `SHOPLOG_API_TOKEN` 时做真实 `initialize` 校验版本；否则验无效 Token 返回 401 JSON，
   若为 302 说明被 `auth_request` 拦截）。

带 Token 的完整验收：

```bash
SHOPLOG_API_TOKEN=<网页设置里的 API Token> bash scripts/deploy.sh
```

## 3. 手动复核（可选）

```bash
curl -s http://127.0.0.1:8088/api/version          # 线上版本号
curl -s https://<域名>/api/version                 # 域名侧是否同样可达
curl -s -X POST https://<域名>/api/mcp -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'   # 应返回 serverInfo.version
```

浏览器侧：打开 `http://127.0.0.1:8088`，右下角徽标应显示当前版本号；
新增记录弹窗默认只显示「传图 / 选已有商品」，传图后自动识别并展开字段。

## 4. 提交与发布顺序（公约）

```
改代码 → run_tests 全绿 → test_mcp 全绿 → deploy.sh 验证通过 → git commit（版本自动 +1）→ 推送
```

推送：`git push gitea main`；GitHub 走 ssh key（`https` 被墙会 502）。
