# ShoppingLog 设计文档

> 版本: 0.4.0
> 日期: 2026-10-24
> 范围: 单用户 → 多用户（注册/登录、每用户独立记录、用户 Token）、网页手工编辑、设置页（通用模型 API + 用户模型 Token）、图文上传记录（skill 与网页双通道）、**SQLite/MySQL 双数据库后端（默认 SQLite）**、删除 admin 内置用户、开源化推送（Gitea 私有 + GitHub 公开）

---

## 1. 目标与范围

ShoppingLog 从「本地个人购物记录库」升级为「可部署的多用户购物记录服务」：

1. **多用户**：注册、登录、每用户独立的数据隔离。
2. **用户 Token**：每个用户拥有独立 Token，供 skill / CLI / API 客户端使用，替代旧的单令牌 Cookie。
3. **网页手工编辑**：浏览器内对记录进行新增、修改、删除。
4. **纯本地图文上传**：网页一次可传多张图片，后端用 macOS Vision framework（`scripts/ocr_vision.swift`）逐张本地 OCR，按规则自动填充名称/品牌/价格/日期/配料/营养等字段，识别结果可继续手动修改；**全程不调用任何模型 API**。
5. **本地健康评分**：纯规则算法（能量/蛋白质/脂肪/糖/钠/膳食纤维/添加剂等）给出 0-100 评分与理由，随记录保存并在卡片展示。
6. **双通道录入**：skill（CLI）通道与网页上传通道。
7. **SQLite/MySQL 双后端**：默认 SQLite（零依赖）；可选 MySQL（专属库 + 受限用户），统一 `db.py` 方言适配层，业务代码零改动切换。
8. **删除 admin 内置用户**：0.2 不再保留默认管理员，数据归属到真实用户。
9. **开源化**：项目推送 Gitea（私有）与 GitHub（公开），不含本地记录数据，含源码 / 建表语句 / 设计文档 / 配置模板 / skill / 中英文 README / 传统与 AI 客户端安装指令。

非目标（0.2 不包含）：公开注册对外暴露（默认关闭注册开关）、多用户社交、迁移现有云端部署数据。

---

## 2. 总体架构

```
浏览器 (site/*.html)
      │  Cookie: shopping_token=<user token>
      ▼
nginx :8088  (反向代理 + 静态资源 + auth_request 鉴权)
      │  /api/* → 127.0.0.1:8787
      │  静态    → site/ 目录（auth_request 通过后）
      ▼
server.py  (HTTP API 服务, 127.0.0.1:8787)
      │  鉴权: Cookie / Authorization: Bearer <token>
      ▼
db.py  (统一数据库层：SQLite / MySQL 方言适配与转译)
      ├── SQLite   data/shopping.db（默认）
      └── MySQL    shoppinglog 库（config.local.json 配置，可选）
      ├── users       用户表（密码哈希、Token、模型 Token）
      ├── settings    系统级设置（通用模型 API）
      └── items       记录主表（+user_id 隔离）
      └── *_items     品类扩展表（food/health/beauty/clothing/electronics）

CLI / skill:
  shop.py add ... --token <USER_TOKEN>
      └── 经 db.py 直连 SQLite/MySQL（本机）或调 API（远端）
```

- 静态资源仍由 nginx 直接 serve（性能好），鉴权用 `auth_request` 交给 server.py 的 `/api/auth-check` 判定。
- 动态数据不再走全量 `data.js`，前端通过 `GET /api/items` 按用户拉取（Cookie 自动携带）。
- `sync_site.py` 保留，用于生成「默认用户」的静态快照（CLI 场景可选）。

---

## 3. 表结构

### 3.1 users（新增）

```sql
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      VARCHAR(64)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,               -- pbkdf2_sha256
    display_name  VARCHAR(64),
    token         VARCHAR(64)  NOT NULL UNIQUE,        -- 用户 Token（skill/API 用）
    is_admin      INTEGER DEFAULT 0,
    is_active     INTEGER DEFAULT 1,                   -- 0=被管理员停用（无法登录）
    deleted_at    VARCHAR(32),                         -- 非空=在回收站（软删除）
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_token ON users(token);
CREATE INDEX IF NOT EXISTS idx_users_deleted ON users(deleted_at);
```

> 0.2 起唯一/主键文本列统一用 `VARCHAR`（SQLite 自动兼容，MySQL 规避 TEXT 键限制）。
> 0.3 起移除 `model_token` 列（本地化改造，不再需要用户级模型 Token）。
> `deleted_at` 用 `VARCHAR(32)` 而非 `TEXT`：MySQL 不允许 TEXT 列直接建索引（1170）。

### 3.2 settings（新增，系统级键值）

```sql
CREATE TABLE IF NOT EXISTS settings (
    skey  VARCHAR(64) PRIMARY KEY,
    value TEXT
);
-- 键示例:
--   allow_register  注册开关（默认 0=关闭, 1=开启）
```

> 0.2 起 `key` 统一改为 `skey`（SQLite 亦同步，避免 MySQL 方言分叉）。
> 0.3 起删除 `model_base_url / model_api_key / model_name` 等模型键。

### 3.3 items（迁移：新增 user_id）

原 `items` 表追加一列：

```sql
ALTER TABLE items ADD COLUMN user_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id);
```

- 旧数据迁移：`UPDATE items SET user_id = <默认用户 id> WHERE user_id IS NULL;`
- 查询一律 `WHERE user_id = ?` 隔离。
- 品类扩展表（food_items / health_items / beauty_items / clothing_items / electronics_items）结构不变，通过 `item_id` 关联主表；主表 `user_id` 即归属判据，扩展表无需冗余 user_id。

### 3.4 完整建表语句

见 `docs/schema.sql`（由 §3.1 + §3.2 + 原 shop.py SCHEMA + §3.3 迁移组合，可独立执行）。

---

## 4. 程序逻辑

### 4.1 模块划分

| 文件 | 职责 |
|------|------|
| `scripts/db.py` | 统一数据库层：SQLite/MySQL 连接、方言转译（`PRAGMA`/`sqlite_master`/`datetime`/`AUTOINCREMENT`/`INSERT OR IGNORE`/`ON CONFLICT`）、引擎选择（env > config > 默认） |
| `scripts/shop.py` | CLI：add/list/search/stats/report/export/update/delete/categories；新增 `--token` 参数与用户隔离 |
| `scripts/server.py` | HTTP API 服务（多用户鉴权、注册登录、CRUD、多图本地 OCR 上传、设置、auth-check） |
| `scripts/sync_site.py` | 默认用户静态快照生成（可选通道） |
| `scripts/migrate.py` | 建表与迁移（幂等，自动适配双引擎；0.2 不再创建 admin 默认用户） |
| `scripts/local_ocr.py` | 本地 OCR 解析与字段提取（macOS Vision `ocr_vision.swift`，零模型 API） |
| `scripts/health_score.py` | 食品健康评分（纯规则算法，能量/蛋白质/脂肪/糖/钠/纤维/添加剂） |
| `site/login.html` | 登录页（JSON fetch 提交 `/api/login`，含注册链接） |
| `site/register.html` | 注册页（JSON fetch 提交 `/api/register`） |
| `site/index.html` | 看板（fetch /api/items、新增/编辑/删除弹窗、设置面板、退出登录） |

### 4.2 API 一览

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET  | `/api/health` | 无 | 健康检查 |
| POST | `/api/register` | 无（受 allow_register 控制） | 注册：`{username, password}` → 返回 token |
| POST | `/api/login` | 无 | 登录：`{username, password}` → Set-Cookie + JSON token |
| POST | `/api/logout` | 无 | 退出登录：服务端下发过期 Cookie（HttpOnly 只能服务端清，且不作废 token） |
| GET  | `/api/auth-check` | 有 | nginx auth_request 用：校验 Cookie/Token，返回 200/401 |
| GET  | `/api/me` | 有 | 当前用户信息（不含敏感字段） |
| GET  | `/api/items` | 有 | 当前用户记录列表（JSON） |
| POST | `/api/add` | 有 | 结构化新增（JSON） |
| POST | `/api/update` | 有 | 修改（`{id, ...字段}`，仅本人记录） |
| POST | `/api/delete` | 有 | 删除（`{id}`，仅本人记录） |
| POST | `/api/upload` | 有 | 图文上传（`images` 可传 base64 或已归档路径），本地 OCR + 健康评分后写入 |
| POST | `/api/ocr` | 有 | 本地 OCR 预览（`images:[base64]` 或 `paths:[已归档路径]`），**不保存**，返回识别字段与健康评分 |
| POST | `/api/images` | 有 | 图片预上传：归档到 `images/YYYY-MM/` 并返回路径，**不建记录** |
| POST | `/api/images/delete` | 有 | 删除单张图片（可带 `item_id` 同步从记录里摘除） |
| GET/POST | `/api/settings` | 有 | 读/写设置：仅系统级 `allow_register`（管理员） |
| GET | `/api/public` | 无 | 公开广场：按商品名分组 + 分页；**只输出商品与评价，不含购买记录**（`PUBLIC_FIELDS` 白名单） |
| POST | `/api/password` | 有 | 修改自己的密码（校验原密码，改后轮换 token） |
| POST | `/api/token` | 有 | 重置自己的 API Token |
| GET | `/api/users` | 管理员 | 用户列表（`?bin=1` 只返回回收站） |
| POST | `/api/users/{active,admin,password,delete,restore,purge}` | 管理员 | 启停 / 设管理员 / 重置密码 / 移入回收站 / 恢复 / 永久删除 |

鉴权方式（二选一）：
- `Cookie: shopping_token=<user.token>`
- `Authorization: Bearer <user.token>`

### 4.3 关键流程

**首次部署引导（0.4 新增）**：`ensure_schema()` 末尾调用 `bootstrap_admin()`——若 `users` 表为空，自动创建一名管理员。
没有这一步会形成死锁：注册默认关闭（`allow_register=0`），库里又没有账号，谁都登录不进来，也没有管理员能去打开注册开关。
密码取 `SHOPLOG_ADMIN_PASSWORD` 环境变量，否则用 `gen_password()` 生成随机强密码（保证大小写+数字+符号齐全，必然通过 `check_password_strength`）。
密码打印到控制台，并写入 `data/INITIAL_ADMIN.txt`（权限 `0600`），登录后应立刻改密并删除该文件。已有用户时直接返回 `None`，幂等。

**注册**：`POST /api/register` → 校验 `allow_register`（默认关闭；本机部署可在 settings 开启或由管理员预建）→ 用户名查重 → `pbkdf2_sha256` 哈希密码 → 生成 32 字节 hex token → 写 users → 返回 `{token, username}`。

**登录**：`POST /api/login` → 校验用户名密码 → 生成/复用 token（0.1 每次登录轮换 token，旧 token 失效）→ `Set-Cookie: shopping_token=<token>; Path=/; HttpOnly; SameSite=Lax; Max-Age=15552000` → 同时返回 JSON `{token}` 供 skill/CLI 使用。
回收站账号（`deleted_at` 非空）返回 401「用户名或密码错误」，不泄露它曾经存在；停用账号返回 403。

**鉴权解析**：`token_from_request()`：优先 `Authorization: Bearer`，其次 Cookie；查 users 得 `user_id`；`is_active=0` 或 `deleted_at` 非空一律视为无效 → 401。

**auth-check（nginx）**：`GET /api/auth-check` 校验通过返回 200（空 body），失败 401。nginx `auth_request` 配置后，静态资源（index.html、图片、js）均以此判定，替代旧的 `map` 单令牌校验。

**数据隔离**：所有 items 查询/写入都带 `user_id` 条件；`delete/update` 先校验 `item.user_id == current_user.id`，否则 403。

**图片先传后关联（0.4 改造）**：表单第一步选图后立刻调 `POST /api/images`，图片即归档到 `images/YYYY-MM/` 并返回相对路径；前端只持有路径，保存记录时把路径数组作为 `images` 提交给 `/api/add` 或 `/api/update`。
- 好处一：**编辑记录也能追加图片**（旧实现只在新增路径里处理图片，编辑时选图会被忽略或产生重复记录）。
- 好处二：OCR 只需 `POST /api/ocr {paths:[...]}`，不用为了识别再传一遍 base64。
- 删除单张：前端点缩略图 ✕ 只是从本地数组移除，保存时 `/api/update` 对比新旧数组，把被移除的**磁盘文件一并删除**（`images_removed` 返回数量）。取消保存则图片保持原样，不会误删。
- 删除整条记录 / 永久删除用户时，关联图片文件同步清理。
- 路径安全：`_abs_image()` 只接受 `images/` 前缀，且 `resolve()` 后必须仍在 `ROOT/images` 下，阻断目录穿越。

**图文上传（本地 OCR）**：`POST /api/upload`（`images` 可为 base64 数组、已归档路径数组，或两者混合）→ 已归档的直接用磁盘文件、base64 的解码存临时图片 → 逐张调 `bin/ocr_vision`（macOS Vision）本地 OCR → `local_ocr.parse_ocr_text` 按规则提取 `name/brand/category/price/purchase_date/ingredients/nutrition/...`（识别不到留空）→ 用户提交字段优先于 OCR 结果 → `health_score.score_food` 纯规则健康评分 → 写库（含 `health_score/health_level/health_reason`）→ 图片按 `images/YYYY-MM/` 规则归档 → 返回新记录与识别/评分详情。全程不调用任何模型 API。

**退出登录**：`POST /api/logout` → 服务端下发 `shopping_token=; Max-Age=0; HttpOnly`。
登录 Cookie 是 HttpOnly 的，**JS 删不掉**，前端自己写 `document.cookie` 会导致「跳走了但一刷新又登录回来」。
退出**不作废 token**——同一 token 还供 CLI / skill 用，网页退出不该把命令行踢下线。

**公开广场（分组 + 分页 + 字段白名单）**：`GET /api/public?page=&size=&q=` 按 `LOWER(TRIM(name))` 分组后分页，**分页对象是商品分组而不是原始行**——否则同一商品会被切到两页，分组展示就失去意义。响应含 `groups[{name,count,owner_count,items[]}]`、`total`、`pages`、`has_next/has_prev`。过滤条件：`visibility='public'` 且作者 `is_active=1` 且 `deleted_at IS NULL`。

输出经 `public_view()` 按 `PUBLIC_FIELDS` **白名单**裁剪：**只给商品属性与评价，不给购买记录**（价格、购买日期、渠道、地点、数量、订单截图、保质期、配料/营养）。用白名单而不是黑名单，是为了以后给 `items` 加字段时默认不泄露。分组层面也不返回 `last_date`。

**按健康档次筛选（0.6.2）**：`GET /api/public?grade=A|B|C|D|none` 在 SQL 层按 `health_score` 区间过滤（阈值来自 `health_score.GRADE_BANDS`）；公开广场页面渲染 A/B/C/D/未评分 chips，个人页在工具栏加「健康」下拉（本地即时筛选）。分页对象仍是商品分组，同款商品不会被切到两页。

**健康评分：服务端唯一权威（0.6.2）**：`/api/add`、`/api/update`（改了 `nutrition` 或 `ingredients`）都调用 `health_score.score_food`，把 `health_score / health_level / health_reason` 写进 `items`；`health_grade`（A≥80 / B≥60 / C≥40 / D<40）由后端换算后随 `/api/items`、`/api/public` 一起下发。前端只展示，不再另算一套分——`site/shared.js` 的 `healthGrade()` 用同一套阈值，`run_tests.py` 有逐档比对用例防漂移。老数据用 `scripts/recalc_health.py` 补分（默认只补未评分，`--all` 全量）。

**用户生命周期（0.4 三级）**：

| 状态 | 触发 | 能否登录 | 数据 | 出现在公开广场 |
|---|---|---|---|---|
| 正常 | 注册 / 恢复 | 能 | 保留 | 是 |
| 停用 | `/api/users/active {active:false}` | 否（403） | 保留 | 否 |
| 回收站 | `/api/users/delete`（软删） | 否（401，按不存在处理） | **保留**，token 轮换 | 否 |
| 已删除 | `/api/users/purge`（仅回收站内可调） | — | **真删**（items + 扩展表 + 图片文件） | — |

硬约束：不能停用/降权/删除自己；至少保留一名未删除的管理员；**未进回收站的账号不能直接 purge**（防止一步误删）。

**设置读写**：`GET /api/settings` 返回系统级（仅 `allow_register`，脱敏）+ 用户级（登录 token 脱敏展示）；`POST /api/settings` 仅支持系统级 `allow_register`（管理员）。模型相关键已全部删除。

### 4.35 MCP 服务（skill / AI 客户端的唯一数据入口）

MCP **内置在服务进程里**：`POST /api/mcp`（JSON-RPC 2.0）。工具逻辑在 `scripts/mcp_tools.py`，
与网页共用同一条鉴权与校验链路（内部回环调用自己的 `/api/*`，杜绝"两套实现漂移"）。

三层结构：

| 文件 | 角色 |
|---|---|
| `scripts/mcp_tools.py` | 工具 schema + 参数整形 + 结果裁剪，**协议无关** |
| `scripts/server.py` → `/api/mcp` | HTTP 传输，MCP 就在服务进程内（**推荐，零子进程**） |
| `scripts/mcp_server.py` | stdio 管道，只做转发；给只支持 stdio 的客户端用 |

> stdio 传输有个绕不开的约束：客户端必须拥有子进程的 stdin/stdout，而常驻服务的 stdout 已接到日志。
> 所以"完全零子进程"只能走 HTTP。现在 HTTP 是默认方式，`mcp_server.py` 退化成 60 行管道。

**为什么要有它**：早期 skill 是直接调 `shop.py` 往数据库里写。那等于把数据库凭据和写权限交给模型——
绕过了鉴权、用户隔离、可见性、密码策略。现在 skill 只能看到「受 Token 约束的接口能力」，
它没有能力去改别人的数据，也没有能力碰数据库文件。

| 项 | 说明 |
|---|---|
| 传输 | **HTTP**：`POST http://127.0.0.1:8088/api/mcp`（推荐）；兼容 stdio 管道 |
| 协议 | JSON-RPC 2.0，`initialize` / `tools/list` / `tools/call` |
| 鉴权 | `SHOPLOG_API_TOKEN` = 网页「设置 → API Token」里那个 token，走 `Authorization: Bearer` |
| 目标地址 | `SHOPLOG_API_URL`，默认 `http://127.0.0.1:8088`（nginx → 8787） |
| 配置位置 | `~/.workbuddy/mcp.json`（**不在仓库内**，含 token，权限 600） |
| 自检 | `python3 scripts/mcp_server.py --selftest`（也可直接 POST /api/mcp） |
| 端到端测试 | `python3 scripts/test_mcp.py`（临时库起真实服务，21 项断言） |

工具清单：

| 工具 | 对应 API | 说明 |
|---|---|---|
| `whoami` | `GET /api/me` | 确认 Token 身份 |
| `list_items` | `GET /api/items` | 关键词 / 分类 / 日期区间过滤 |
| `get_item` | `GET /api/items` | 按 id 取单条 |
| `add_item` | `POST /api/add` | 新增，`name` + `price` 必填，其余走 `item` 透传 |
| `update_item` | `POST /api/update` | 只改传入字段 |
| `delete_item` | `POST /api/delete` | 连同图片文件一起删 |
| `upload_images` | `POST /api/images` | 本地图片 → 服务端归档，返回路径 |
| `public_feed` | `GET /api/public` | 公开广场（自动剔除购买记录） |
| `stats` | `GET /api/items` 聚合 | 总数 / 总花费 / 分类 / 按月 |

**边界**：MCP 服务**不直接读写数据库、不碰 `images/`、不生成静态文件**。它只发 HTTP 请求。
所以服务端做的一切校验（权限、可见性、密码、回收站）对 skill 同样生效。

**端口约束（重要）**：公网/域名只转发 **8088**，后端 8787 仅本机可达 —— 所以 MCP **必须挂在 8088 上**
（`https://<域名>/api/mcp`），不能另起端口，否则域名访问不到。nginx 里要给 `/api/mcp` 单独放行：

| 要点 | 原因 |
|---|---|
| `/api/mcp` 不走 `auth_request` | auth_request 子请求默认拿不到 Bearer，无效 Token 会被 302 成登录页，MCP 客户端收到 HTML |
| 显式 `proxy_set_header Authorization $http_authorization` | 保证 Bearer 透传到后端（后端自己判 Token，无效返回 401 JSON） |
| `proxy_read_timeout 300s` | 多图 OCR / 大 payload 较慢，默认 60s 会截断 |

### 4.36 版本号（唯一来源：仓库根 `VERSION`）

| 项 | 说明 |
|---|---|
| 文件 | 仓库根 `VERSION`（如 `0.5.3`），`scripts/version.py` 读写它 |
| 递增 | `bash scripts/bump_version.sh`（patch，第三位 +1）；`minor` / `major` 需明确要求 |
| 自动 | `scripts/hooks/pre-commit`：提交时若 `VERSION` 没变过就自动 patch +1 并加入提交 |
| 出口 | `GET /api/health`、`GET /api/version`（免鉴权）、MCP `initialize.serverInfo.version` |
| 页面 | `index.html` / `public.html` 右下角徽标，读 `/api/version` |
| 前端展示层 | 两页共用 `site/shared.js`（`esc` / `money` / `starStr` 半星 / `healthScore` / `nutriTable` …），新增展示字段改这一处即可两页同步 |
| 部署校验 | `deploy.sh` 断言「源码 `VERSION` == 线上 `/api/version`」，并复核 8088 上的前端与 `/api/mcp` |

> 判据：`curl -s http://127.0.0.1:8088/api/version` 与仓库 `VERSION` 不一致 → 就是没部署上去。

### 4.37 商品档案 vs 购买记录（0.6.0 起的存储模型）

| | `items` | `purchases` |
|---|---|---|
| 语义 | 商品档案（名称/品牌/成分/营养/评价/图片/可见性） | 每一次购买（日期/价格/数量/渠道/地点/折扣） |
| 关系 | 1 | N（同一商品买 N 次 → N 行） |
| 例子 | 三只松鼠每日坚果 ×1 条 | 618 买过、双 11 又买 → 2 行 |

为什么拆：`items` 上直接挂购买字段时，同款买 N 次要复制 N 份商品信息，价格与评价会互相打架，
也算不出「这件东西历史上多少钱」。拆开后商品只有一份、购买事实按次追加，
`purchase_count / total_spent / avg_price` 直接由 `purchases` 聚合出来。

兼容策略：`items` 上的 `price / purchase_date / quantity / channel / place` 及各价格列保留为
**最近一次购买的冗余缓存**（写入时与 `purchases` 同步），好处是排序、统计、公开广场、
CLI 与既有测试全部不用改；历史一律以 `purchases` 为准。旧库启动时自动回填首条购买记录（幂等）。

API 语义：
- `POST /api/add` 带 `item_id` → **复购**，只插 `purchases`（未传的购买字段沿用上一次）；
  不带 `item_id` → 建商品 + 首条购买。
- `POST /api/update` 改购买字段时同步最近一次购买（或 `purchase_id` 指定那次）。
- `POST /api/delete` 删除商品时级联删 `purchases`。

### 4.4 CLI / skill 通道

- `shop.py` 全部命令增加 `--token <USER_TOKEN>`：
  - 本机模式（默认）：直连 SQLite，但先校验 token 存在且有效，所有查询按 `token → user_id` 过滤。
  - 远端模式（`--api http://host:8088 --token <USER_TOKEN>`）：请求 server.py API，token 走 `Authorization: Bearer`。
- 旧「无 token 直接操作本地库」在 0.1 保留为管理员兼容（`--no-auth` 显式声明，仅限本机文件可写场景），仓库默认禁止无 token 写入。

### 4.5 安全设计

- 密码不落明文：`pbkdf2_sha256`（每用户随机盐）。
- Token 不落日志；登录响应与 `Set-Cookie` 中的 token 仅在 HTTPS/本机回环下信任。
- 删除/修改必须属于当前用户（越权 403）。
- **软删除先于硬删除**：用户删除必须两步（移入回收站 → 永久删除），永久删除还会二次确认并明确告知将删除记录与图片。
- **图片路径白名单**：所有接受图片路径的接口都过 `_abs_image()`，只认 `images/` 前缀且 `resolve()` 后仍在 `ROOT/images` 内，阻断 `../` 目录穿越。
- 回收站账号登录返回 401「用户名或密码错误」，不暴露账号曾经存在；停用账号返回 403。
- 默认管理员密码只落盘一次（`data/INITIAL_ADMIN.txt`，0600），登录后要求立即改密。
- nginx 层静态资源鉴权由 auth_request 统一判定，不再维护静态 token map。
- 开源仓库不包含：`shopping.db`、`data.js`、`images/`、`.htpasswd`、nginx 私密配置、真实 token/密码、本机绝对路径。

---

## 5. 展示设计

### 5.1 登录页（login.html）与注册页（register.html）

- 独立页面，JSON fetch 提交，与后端 JSON 接口保持一致。
- 登录成功：跳转 `/`（看板），服务端 `Set-Cookie` 自动携带，无需前端存 token。
- 注册：用户名 + 密码 + 确认密码，前端校验密码长度与一致性；成功跳回登录页。
- 注册开关关闭（`allow_register=0`）时后端拒绝注册，前端提示联系管理员。

### 5.2 看板（index.html）

- 数据源从 `data.js` 静态全量改为 `fetch('/api/items')`（Cookie 鉴权），失败 401 时跳登录页。
- 原有卡片分组、口味 tab、搜索、统计、导出等交互保持。
- 新增：
  - 顶部工具栏：**＋ 新增记录**（三步式弹层表单）与 **🌐 公开广场**。
  - **三步式表单**：① 上传图片（可多选，**选完即上传归档**，缩略图可单张 ✕ 删除）→ ② 核对并补全（OCR 只填空字段、填过的标绿「已识别」；控件分型：文本框 / 数字 / 日期选择器 / 下拉选项）→ ③ 分享范围（公开 / 私有 + 分享备注）。
  - 每张卡片「编辑」按钮：弹层表单回填字段（含已有图片缩略图，可增可删），提交 `/api/update`。
  - 每张卡片「删除」按钮：确认后 `/api/delete`（连同图片文件）。
  - 右上角用户菜单：显示用户名、`设置`、`退出登录`（清除 Cookie/localStorage 跳登录页）。
  - **公开广场**：**独立页面 `public.html`，新标签页打开，免登录可访问**。按商品名聚合，同一商品并列展示不同用户的评分/评价/分享备注/健康评分；底部分页条，搜索走后端 `q` 参数。页面顶部明确写出「购买记录不会公开」。

### 5.3 设置面板（index.html 弹窗）

- 打开方式：看板页头「⚙️ 设置」按钮弹出。
- 账号区：用户名、账号类型、创建时间、Token 明文（可复制 / 可重置）。
- 修改密码：原密码 + 新密码 + 确认；前端提示强度规则，后端强校验。
- 系统级（管理员可见）：「开放注册」开关。
- **用户管理（管理员）**：分「全部用户 / 回收站」两个页签。
  - 全部用户：停用/启用、设为/取消管理员、重置密码、**移入回收站**。
  - 回收站：**恢复**、**永久删除**（二次确认，明确提示会删除记录与图片）。
- 已移除全部模型配置（API 地址 / 模型名 / API Key / 用户模型 Token）。
- 保存后提示成功并回显脱敏值。

### 5.4 响应式与缓存

- 保持原 gzip + no-cache 策略；`/api/items` 动态数据由 API 返回，天然无缓存问题。
- 图片仍走 nginx 静态（auth_request 保护），`expires 7d`。

---

## 6. 部署与安装

### 6.1 传统安装（本机 / 服务器）

```bash
# 1. 克隆
git clone <gitea-or-github-url> ShoppingLog && cd ShoppingLog
# 2. 建库（默认 SQLite；如需 MySQL 先建专属库与受限用户并写 config.local.json，见 docs/MYSQL.md）
python3 -m venv .venv && source .venv/bin/activate
python3 scripts/migrate.py          # 建表 + 补列 + 首次创建默认管理员
# 3. 配置 nginx（参考 docs/nginx.shopping.conf.example）
# 4. 启动 API 服务
nohup python3 scripts/server.py --port 8787 >> logs/server.log 2>&1 &
# 5. 浏览器访问 http://localhost:8088
```

**首次部署的账号从哪来？**
`migrate.py` / `server.py` 启动时若发现 `users` 表为空，会自动创建一名管理员，并把用户名密码打印到控制台、同时写入 `data/INITIAL_ADMIN.txt`（权限 `0600`）。
也可以自己指定密码：

```bash
SHOPLOG_ADMIN_PASSWORD='YourStr0ng!Pass' python3 scripts/migrate.py
```

登录后**立刻修改密码**，然后删掉 `data/INITIAL_ADMIN.txt`。
之后的账号由管理员开启「开放注册」让用户自助注册，或在设置面板里创建。

**让服务常驻（推荐）**：

```bash
bash scripts/install-service.sh            # 注册 macOS LaunchAgent：开机自启 + 崩溃自动拉起
bash scripts/install-service.sh --status   # 查看状态
bash scripts/install-service.sh --uninstall
```

脚本会自动挑选「装了 pymysql 的解释器」（MySQL 模式必需），并在注册前停掉占用端口的旧进程。
`launchctl` 在受限沙箱里会返回 `Bootstrap failed: 5: Input/output error`，必须在普通终端执行。

引擎选择：`config.local.json`（`db.engine`）或环境变量 `SHOPLOG_DB_ENGINE=sqlite|mysql`；缺省 SQLite。

### 6.2 AI 客户端安装（Codex / WorkBuddy 等）

> 将以下段落粘贴给 AI 客户端，即可完成项目认知与配置：

```
项目: ShoppingLog —— 多用户购物记录服务。
目录: <项目绝对路径>
技术栈: Python3 + SQLite/MySQL + 原生 HTML/JS + nginx。
启动: python3 scripts/server.py --port 8787（API），nginx 监听 8088 反代。
数据库: 默认 data/shopping.db（SQLite）；config.local.json 可切换 MySQL；统一走 scripts/db.py。
CLI: scripts/shop.py add/list/update/delete ... 必须带 --token <用户Token> 或 --api + --token。
建表/迁移: python3 scripts/migrate.py（幂等，自动适配双引擎）。
设计文档: docs/DESIGN.md；建表语句: docs/schema.sql；MySQL 手册: docs/MYSQL.md。
我的用户 Token: <USER_TOKEN>（在设置页查看/重置）。
请基于以上信息回答我的购物记录相关问题。
```

---


## 7. 版本记录

> 逐条细节见 [CHANGELOG.md](CHANGELOG.md)，本节只记设计层面的取舍。

- **0.6.2 (2026-10-24)**：购买记录拆表（`purchases`，一次购买一行 + 旧库幂等回填）；
  传图录入改成真正两步（传图即自动识别、识别完才展开表单）；新增「再买一次」（弹窗搜商品 / 卡片 🛒）；
  卡片购买记录块改读 `purchases` 并显示次数与均价。详见 [CHANGELOG.md](CHANGELOG.md)。
- **0.5.3 (2026-10-24)**：
  - **版本号单一来源**：新增仓库根 `VERSION` 文件与 `scripts/version.py`；`/api/health`、`/api/version`、
    MCP `initialize.serverInfo.version` 全部改读它，不再各处硬编码（此前 `server.py` 与 `mcp_tools.py`
    各写一个字符串，容易漂移）。
  - **提交即递增第三位**：新增 `scripts/bump_version.sh [patch|minor|major]` 与 `scripts/hooks/pre-commit`
    （忘记改也会自动 patch +1 并 `git add VERSION`）；第二位/第一位只在明确要求时才动。
  - **网页右下角版本徽标**：`index.html` / `public.html` 各加一个 `v0.5.x` 小徽标，读免鉴权的新接口
    `GET /api/version`，一眼看出线上跑到哪了。
  - **8088 单端口可达性加固**：nginx 新增 `location = /api/mcp`（免 `auth_request` + 透传 `Authorization`
    + 300s 超时）——此前无效 Token 会被 302 成登录页 HTML，MCP 客户端拿不到 JSON-RPC 错误；
    `/api/version` 免鉴权放行；`/api/` 与 `/api/mcp` 都加 300s 代理超时。
  - **`deploy.sh` 复核加严**：断言源码 `VERSION` == 线上 `/api/version`，并在 8088 上复核前端、
    `/api/version`、`/api/mcp`（带 Token 时做真实 `initialize` 校验版本，否则验无效 Token 返回 401 JSON）。
  - **测试**：`run_tests.py` 新增 2 项（`/api/version` 与 `VERSION` 一致且免鉴权；MCP 版本一致 +
    无效 Token 401），共 34 项全绿。
- **0.5.2 (2026-10-24)**：
  - **「我的记录」卡片修复与放大**：删除/编辑按钮改成同一条 flex 操作条（原两个按钮各自绝对定位，窄卡片上重叠）；
    卡片主图 `aspect-ratio:4/3` 撑满卡片宽度。根因是 `.thumb` 类被「卡片主图」和「上传预览小图」复用，
    后者的 `74px` 规则覆盖了前者 —— 预览小图已改名为 `.mini`。
  - **折叠块重排**：营养成分+配料表 / 食用方式 / 其他信息 / **购买记录**；
    生产日期、保质期、到期日期统一归入「购买记录」，不再与食用方式混排。
  - **公开广场改小红书式大图卡片流**：3:4 大图 + 作者/星级/评价/标签，单条记录用「左图右文」主推卡；
    图片可点开查看、多图翻页。默认折叠展示营养成分+配料表（含 NRV%）、食用方式、其他信息。
  - **公开字段策略调整**：`PUBLIC_FIELDS` 增加 `images / ingredients / nutrition / nutrition_analysis /
    eating_method / manufacturer / manufacturer_address / origin / specs / tips / allergens /
    storage_condition / standard_no / shelf_life`；`produced_date`、`expiry_date` 仍不公开（属购买记录）。
  - **新增 `GET /api/public-image?p=`**：免登录页的图片出口。nginx 对 `/images/*` 有登录鉴权，
    公开页直连会被 302 成登录页（浏览器表现为"图片空白"）；该接口逐张校验图片确实属于某条公开记录才流出。
- **0.5.1 (2026-10-24)**：
  - **MCP 内置进服务进程**：新增 `POST /api/mcp`，工具逻辑抽到 `scripts/mcp_tools.py`；
    `mcp_server.py` 从 525 行降为纯 stdio 管道（只转发）。默认配置改为 HTTP，**不再有额外子进程**。
  - `/api/mcp` 与其它接口同一套鉴权（Bearer / Cookie），内部回环复用 `/api/*`，不存在第二套校验。
  - `scripts/test_mcp.py` 增加 HTTP 传输用例（含无 token 401、通知 202、批量消息）。
- **0.5.0 (2026-10-24)**：
  - **新增 MCP 服务** `scripts/mcp_server.py`（9 个工具），skill 的数据读写从「直连数据库」改为「走 MCP → HTTP API」。
  - `shopping-log` 技能重写：删除本地 markdown/索引/data.js/腾讯文档同步，改为纯 MCP 调用；补「安全红线」。
  - 新增 `scripts/test_mcp.py`：临时库起真实服务 + stdio 协议驱动，21 项断言。
  - **协作公约新增步骤 4.7**：必须先 `scripts/deploy.sh` 升级并验证线上可用，才允许提交/推送源码。
  - 版本号 0.4.1 → 0.5.0（`/api/health` 可据此判断线上跑的是哪一版）。
- **0.4.1 (2026-10-24)**：
  - **修复线上功能全部 404**：代码写完但服务未重启，`/api/token`、`/api/password`、`/api/users` 全返回 not found。已迁移生产库（MySQL 补 4 列）并重启至 0.4.0。
  - **退出登录**：新增 `POST /api/logout`。原实现用 JS 删 HttpOnly Cookie，实际无效 → 页面跳走但一刷新又登录回来。
  - **Token 明文**：设置面板显示完整 Token（原来只有脱敏值，复制出来是星号），复制改为读内存变量而非 DOM。
  - **公开广场独立成页**：新增 `public.html`，免登录、新标签页打开；nginx 精确 location 放行 `/public.html` 与 `/api/public`。
  - **公开字段白名单**：`/api/public` 不再返回价格/购买日期/渠道/地点/数量/图片等购买记录。
  - **升级顺序修复**：`ensure_schema()` 先补列再执行 `schema.sql`，否则旧库会在建索引处中断（`Key column doesn't exist`）。
  - 新增 `scripts/install-service.sh`（LaunchAgent 常驻）。
- **0.4.0 (2026-10-24)**：
  - **首次部署引导**：`bootstrap_admin()` 在空库时自动建管理员，密码打印并落盘 `data/INITIAL_ADMIN.txt`（0600）；`migrate.py` 改为复用 `server.ensure_schema()`，不再维护第二份 DDL。
  - **图片流程改造**：新增 `POST /api/images` 预上传（选图即归档）、`POST /api/images/delete` 删单张；`/api/add` 与 `/api/update` 接受已归档路径数组；`/api/update` 对比新旧数组清理被移除的磁盘文件；`/api/ocr` 支持 `paths` 直接识别已归档图片。**编辑记录可追加图片**。
  - **公开广场分页**：`/api/public` 改为按商品名分组 + 分页（`page/size/q`），分页对象是商品组而非原始行；前端加翻页条与服务端搜索。
  - **用户三级生命周期**：`users.deleted_at` 新增；停用 → 回收站（软删，数据保留）→ 永久删除（仅回收站内可调，真删记录/扩展表/图片）。回收站账号登录按「不存在」处理，公开广场同步隐藏。
  - 用户管理面板加「全部用户 / 回收站」页签。
- **0.2.0 (2026-10-24)**：删除 admin 内置用户（数据迁移至真实用户）；新增统一数据库层 `scripts/db.py`，SQLite/MySQL 双后端（默认 SQLite、方言自动转译、引擎可用环境变量覆盖）；本地配置 `config.local.json`（连接信息不入库）+ `config.example.json` 模板；`settings.key` 改名 `skey`、文本键列改 `VARCHAR` 兼容 MySQL；`docs/schema.sql`、`docs/DESIGN.md` 同步双引擎；新增 `docs/MYSQL.md` 部署手册。
- **0.1.0 (2026-10-24)**：多用户基础（注册/登录/Token/数据隔离）、网页手工编辑、设置页（通用模型 API + 用户模型 Token）、图文上传（模型识别）、双通道录入、设计文档/建表语句/skill/中英文 README、Gitea 私有 + GitHub 公开推送。
