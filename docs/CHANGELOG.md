# 版本更新记录

版本号唯一来源：仓库根 `VERSION` 文件。每次提交递增第三位（patch）；第二位 / 第一位只在明确要求时才升。
递增命令：`bash scripts/bump_version.sh`（`patch` 默认 / `minor` / `major`）。

---

## 0.6.10（2026-10-30）

**常驻方式改由 supervisord 托管（不再自己 nohup / double-fork）**

- 服务纳管：新增 `~/myapp/supervisor/conf.d/shoppinglog.ini`（符合本机 supervisord 的
  `conf.d/*.ini` 约定），进程名 `shoppinglog`，autostart + autorestart，
  日志仍写 `~/myapp/shoppinglog/logs/server.log`。supervisord 自身由 launchd 托管，
  所以整套链路是「开机自启 → supervisord → 各服务」。
- `deploy.sh`：重启前先识别托管方式，**supervisord 优先**（`supervisorctl restart shoppinglog`；
  第一次纳管时先放掉旧进程再 `reread + update`），其次 LaunchAgent（`launchctl kickstart`），
  最后是双 fork 守护进程兜底。健康检查改成轮询（最多 ~30s），DEPLOY_OK 会打印托管方式。
- `install-service.sh`：检测到 shoppinglog 已在 supervisord 里时直接让位并打印 `status`，
  避免 LaunchAgent 与 supervisord 两套托管抢 8787 端口。
- 仓库模板：`docs/supervisor.shoppinglog.ini.example`；README / README.en.md 第 5 步
  改成 supervisord 优先，保留 LaunchAgent 作为无 supervisord 机器的退化方案。
- 套路词清理：修正多处误植入的 "supervisorctl -c ~/myapp/supervisor/supervisord.conf" 尾缀。

## 0.6.9（2026-10-30）

**开机自启：LaunchAgent 托管链路补齐**

- `install-service.sh`：写 `~/Library/LaunchAgents/com.meria.shoppinglog.plist`
  （RunAtLoad + KeepAlive + 崩溃自动拉起），解释器自动挑「装了 pymysql 的那个」。
  之前脚本一遇 `launchctl bootstrap` 失败就把服务停在「已 kill 未重启」的状态，
  现在注册失败会先用守护进程方式把服务原样拉回来，并打印该在普通终端执行的两行注册命令。
- `deploy.sh`：检测到 LaunchAgent 已注册时改用 `launchctl kickstart -k` 重启，
  不再 kill 后另行双 fork（否则 KeepAlive 立刻重建的实例会与 deploy 起的进程抢 8787 端口，
  托管身份也被破坏）；没注册时行为不变。
- README：版本号同步到 0.6.9，第 5 步补 `--status` / `--uninstall` 与「必须在普通终端执行」提示。

## 0.6.7（2026-10-28）

**修：品类扩展表列漂移导致记录列表直接 500**

- 现象：打开个人页 / 调 `GET /api/items`、`GET /api/public` 时连接被服务端直接断开
  （客户端表现为 `RemoteDisconnected`）。任何有记录的账号都中招。
- 根因：品类扩展表的列定义有两份 —— `scripts/shop.py` 的 `EXT_SCHEMAS`（查询投影由它
  生成）和 `docs/schema.sql`（建表语句）。`beauty_items` 在 schema.sql 里少了
  `allergens / eating_method / standard_no` 三列，而 LEFT JOIN 的投影按 `EXT_SCHEMAS`
  生成，于是 SQLite 报 `no such column: beauty_items.allergens`。
- 修法：
  - `docs/schema.sql` 的 `beauty_items` 补齐三列；
  - `ensure_schema()` 增加「按 `shop.EXT_SCHEMAS` 给扩展表补列」的兜底，旧库（含两份
    定义不同步的库）启动时自动自愈，以后只改代码定义也不会再炸。
- 测试：新增 2 个用例（两份列定义一致性静态比对；美妆类字段存读回归），47 → 49 个，全绿。

## 0.6.6（2026-10-28）

- 运行环境升级到 Python 3.14（uv 管理）：`run.sh` / `deploy.sh` / `install-service.sh`
  的候选解释器首位改为 uv 的 `python3.14`，MySQL 模式依赖（pymysql）已在公共 site-packages 里。
- 终端 `python3` 默认指向 3.14.7（`~/.zshrc` 前置 `~/.local/bin`）。

## 0.6.5（2026-10-27）

- 仓库治理：压缩全部历史为单次提交（0.6.5 全量快照），旧历史已备份成 git bundle；
  删除早期 Markdown 方案的残留目录，运行数据统一留在 `~/myapp/shoppinglog`。

## 0.6.2（2026-10-24）

**健康评分收敛到后端 + 按健康档次筛选（A/B/C/D）**

- **健康评分改为服务端唯一权威**：`/api/add`、`/api/update`（改了营养或配料）都会调用
  `health_score.score_food` 并把 `health_score / health_level / health_reason` 写进库。
  以前只有走 OCR 的 `/api/upload` 才算分，MCP / 手工录入的记录三个字段全是 NULL。
  显式传 `health_score` 时以调用方为准（人工复核优先）。没有营养也没有配料时不硬凑分（非食品）。
- **新增 `health_grade`（A/B/C/D）**：后端按 `health_score` 统一换算（阈值 A≥80 / B≥60 / C≥40 / D<40），
  `site/shared.js` 的 `healthGrade()` 用同一套阈值，并有测试逐档比对防止两处漂移。
  档位名称同步为「优秀 / 良好 / 一般 / 需注意」。
- **按档次筛选**：
  - 个人页（`index.html`）工具栏新增「健康」下拉（全部 / A / B / C / D / 未评分），本地即时筛选；
  - 公开广场（`public.html`）新增 A/B/C/D/未评分 chips，走后端 `GET /api/public?grade=`；
  - MCP `list_items` 新增 `grade` 参数（A/B/C/D/none），返回值带 `health_grade`。
- **老数据补分**：新增 `scripts/recalc_health.py`（默认只补算未评分的，`--all` 全量重算，`--dry-run` 预演）。
- **修**：`/api/add`、`/api/update` 直接传 dict/list 形式的 `nutrition / flavors / purchase_history`
  会让 SQLite/MySQL 绑定失败、连接被直接断开（表现为 RemoteDisconnected）。新增 `jsonify_fields()` 统一序列化。
- 测试：新增 5 个用例（新增自动算分、无数据不给分、编辑重算、公开广场档次筛选、前后端阈值一致）。

**购买记录拆表 + 传图录入改造**

- **购买记录独立成表 `purchases`**：商品档案 `items` 与购买记录 1:N，一次购买一行。
  同款买 N 次不再复制 N 份商品信息，价格历史、复购次数、均价、总花费可直接算。
  - 旧库自动回填：每条已有 `items` 视为「买过一次」，生成首条 `purchases`（幂等，可重复执行）。
  - `items` 上的 `price / purchase_date / channel` 等保留为「最近一次购买」的冗余缓存，随写入同步。
  - `GET /api/items` 额外返回 `purchases / purchase_count / total_spent / avg_price / min_price / max_price / first_purchase_date / last_purchase_date`。
  - `POST /api/add` 传 `item_id` 即**复购**：只新增购买记录，未传的购买字段沿用上一次。
  - `POST /api/update` 改购买字段时同步最近一次购买（或 `purchase_id` 指定的那次）；删除商品级联清理购买记录。
  - `POST /api/purchase/delete` 删掉误记的**单次购买**，商品档案保留，缓存刷新为剩余里最新的那条；
    卡片购买记录每行带 ✕ 按钮。
  - MCP `add_item` 新增 `item_id` 参数，复购同样只新增购买记录。
- **传图录入改成真正两步**：选图（支持多选 / 拖拽）→ 图片立即归档并**自动本地识别**，
  识别完才展开第二步表单（识别到的字段标绿），不再需要手动点「识别并填充」。
- **复购入口**：新增弹窗里可搜索已有商品「再买一次」；卡片操作条新增 🛒 按钮，
  选中后只填本次购买信息（价格 / 日期 / 数量 / 渠道），商品档案不重复创建。
- 卡片「购买记录」折叠块改为读取 `purchases`，显示次数 / 合计 / 均价与每次购买明细。

## 0.6.1（2026-10-24）

- 前端展示层抽出 `site/shared.js`（esc / money / 半星 starStr / healthScore / healthGrade / nutriTable 等），
  个人页与公开广场共用一份实现，修掉公开广场 3.5 星显示成 4 星、营养表单位不一致的问题。
- 同步补文档与测试。

## 0.5.3（2026-10-24）

- 版本号单一来源（`VERSION` + `scripts/version.py` + `bump_version.sh` + pre-commit 钩子）。
- 网页右下角版本徽标；新增免鉴权 `GET /api/version`。
- nginx 8088：`/api/mcp` 单独放行并透传 `Authorization`（无效 Token 返回 401 JSON，不再是登录页 302）；
  `/api/version` 免鉴权；代理超时 300s。
- `deploy.sh` 校验版本一致性 + 8088 前端 / MCP 复核。
- 修复 macOS bash 3.2「`$VAR` 紧邻中文全角括号」被当成变量名一部分的隐患。

## 0.5.2（2026-10-24）

- 公开广场改为小红书式大图卡片（商品图、成分、营养、食用方式公开；购买记录仍不公开）。
- 卡片按钮不重叠（操作条 flex）、卡片主图放大到 4:3。
- 新增 `GET /api/public-image`：免登录图片出口，逐张校验图片确属公开记录。

## 0.5.1（2026-10-24）

- MCP 内置进服务进程（`POST /api/mcp`），不再单独起进程；`mcp_server.py` 退化为 stdio 管道。

## 0.5.0（2026-10-24）

- 新增 MCP 服务与工具集（9 个工具）；`shopping-log` 技能改为纯 MCP 调用，不再直连数据库。

## 0.4.x（2026-10-24）

- 用户生命周期：停用 → 回收站 → 恢复 → 永久删除；管理员自保护。
- 公开广场跨用户聚合；图片归档与目录穿越防护。

## 0.3.0（2026-10-24）

- 纯本地多图 OCR（macOS Vision）自动填充字段且全字段可编辑。
- 本地规则食品健康评分（`health_score / health_level / health_reason` 随记录保存）。
- 删除全部模型设置，全程零模型 API。

## 0.2.0（2026-10-24）

- 删除 admin 内置用户；统一数据库层 `scripts/db.py`（SQLite / MySQL 双引擎，方言自动转译）。
- 本地配置 `config.local.json` + 模板 `config.example.json`；新增 `docs/MYSQL.md`。

## 0.1.0（2026-10-24）

- 多用户基础（注册 / 登录 / Token / 数据隔离）、网页编辑、图文上传、双通道录入。
- 设计文档 / 建表语句 / skill / 中英文 README，推送 Gitea 私有 + GitHub 公开。

---

更细的设计取舍见 [DESIGN.md](DESIGN.md)，接口细节见 [api.md](api.md)。
