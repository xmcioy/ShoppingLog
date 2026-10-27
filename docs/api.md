---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 5c462fda08d022c3bf06f0c7c13f492d_e465bd46cf9a11f1acb8525400638852
    ReservedCode1: KskV/XUctttPpuOPZJG1ePQWuB7fPZxiC/WU0uw1PNyrLyaqF/FBVLGTED8wMqU+xzte2jmPlvXRpsgblvacl38+X64/5Ciepei1WTB0i97ocFDRQ4C9DTzCuPXDRfsn0teCDU1Ab7yyFSlyUrmo6uEJ5mqCuFI8KZ5+m0EQYc9fBw/1kdsRW1Wx9s8=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 5c462fda08d022c3bf06f0c7c13f492d_e465bd46cf9a11f1acb8525400638852
    ReservedCode2: KskV/XUctttPpuOPZJG1ePQWuB7fPZxiC/WU0uw1PNyrLyaqF/FBVLGTED8wMqU+xzte2jmPlvXRpsgblvacl38+X64/5Ciepei1WTB0i97ocFDRQ4C9DTzCuPXDRfsn0teCDU1Ab7yyFSlyUrmo6uEJ5mqCuFI8KZ5+m0EQYc9fBw/1kdsRW1Wx9s8=
---

# ShoppingLog HTTP API 设计（v0.2）

## 1. 基础约定

- **Base URL**：`http://127.0.0.1:8787`（nginx 反向代理对外 `http://127.0.0.1:8088`，`/api/*` 转发到后端）。
- **对外只有 8088**：公网/域名只转发 8088，8787 仅本机可达。因此**前端页面与 `/api/mcp` 都必须挂在 8088 上**
  （nginx 反代负责），不能给 MCP 另开端口，否则域名访问不到。
- **内容类型**：请求 `Content-Type: application/json`；响应均为 JSON。
- **鉴权**：二选一
  - `Authorization: Bearer <token>`
  - Cookie `shopping_token=<token>`（登录接口下发，HttpOnly，180 天）
  - 未带有效令牌的受保护接口返回 `401 {"ok": false, "error": "未登录"}`
- **统一响应**：成功 `{"ok": true, ...}`；失败 `{"ok": false, "error": "<原因>"}`，错误码 400/401/403/404/409/500。

## 2. 接口总览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | /api/health | 无 | 健康检查（含版本号） |
| GET | /api/version | 无 | 只返回版本号（网页右下角徽标 / deploy 校验用） |
| GET | /api/auth-check | 可选 | 令牌有效性检查 |
| GET | /api/me | 必选 | 当前用户信息 |
| GET | /api/items | 必选 | 当前用户全部记录 |
| GET | /api/public | 无 | 公开广场：按商品名分组 + 分页（`page/size/q`，`flat=1` 返回原始行）；**只返回可共享字段** |
| GET | /api/public-image | 无 | 公开广场图片出口（`?p=images/...`）；只流出属于公开记录的图片，其余 403/404 |
| GET | /api/users | 管理员 | 用户列表（`?bin=1` 只返回回收站；不含密码哈希） |
| GET | /api/settings | 必选 | 系统与用户设置（脱敏） |
| POST | /api/register | 无 | 注册（需系统开关，含密码强度校验） |
| POST | /api/login | 无 | 登录，返回 token + Set-Cookie |
| POST | /api/logout | 无 | 退出登录：服务端下发过期 Cookie（HttpOnly 只能服务端清） |
| POST | /api/mcp | 必选 | MCP（JSON-RPC 2.0）端点，9 个工具；skill / AI 客户端用 |
| POST | /api/add | 必选 | 新增记录（传 `item_id` 为复购：只加一条购买记录） |
| POST | /api/update | 必选 | 修改记录 |
| POST | /api/delete | 必选 | 删除记录（级联清理其购买记录） |
| POST | /api/purchase/delete | 必选 | 删除单条购买记录（商品档案保留） |
| POST | /api/upload | 必选 | 图文上传（本地 OCR + 健康评分后落库） |
| POST | /api/ocr | 必选 | 本地 OCR 预览（`images` 或 `paths`，不保存） |
| POST | /api/images | 必选 | 图片预上传：归档并返回路径，不建记录 |
| POST | /api/images/delete | 必选 | 删除单张图片（可带 `item_id` 同步摘除） |
| POST | /api/password | 必选 | 修改自己的密码（返回新 token） |
| POST | /api/token | 必选 | 重置自己的 API Token |
| POST | /api/users/active | 管理员 | 启用/停用用户 `{id, active}` |
| POST | /api/users/admin | 管理员 | 设置/取消管理员 `{id, admin}` |
| POST | /api/users/password | 管理员 | 重置他人密码 `{id, new_password}` |
| POST | /api/users/delete | 管理员 | **移入回收站**（软删除，数据保留）`{id}` |
| POST | /api/users/restore | 管理员 | 从回收站恢复 `{id}` |
| POST | /api/users/purge | 管理员 | **永久删除**（仅回收站内可调，真删记录与图片）`{id}` |
| POST | /api/settings | 必选 | 更新设置（管理员/本人） |

### 健康评分（服务端唯一权威）

- 评分规则只有一份：`scripts/health_score.py`（纯规则算法，不调模型）。
  `POST /api/add`、`POST /api/update`（改了 `nutrition` 或 `ingredients`）都会算好并把
  `health_score`（0–100）、`health_level`（优秀/良好/一般/需注意）、`health_reason`（理由摘要）写进库；
  **前端只展示，不再自己算分**，`site/shared.js` 的 `healthGrade()` 与后端阈值一致并有测试逐档比对。
- `health_grade`（A/B/C/D）由后端按 `health_score` 换算：A≥80、B 60–79、C 40–59、D<40，
  `/api/items` 与 `/api/public` 都会带上，用于按档次筛选。
- 没有营养也没有配料的记录（多半是非食品）**不给分**，三个字段留空；调用方显式传
  `health_score` 时以调用方为准（人工复核优先）。
- 老数据补分：`python3 scripts/recalc_health.py`（默认只补算未评分的，`--all` 全量，`--dry-run` 预演）。
- MCP `list_items` 新增 `grade` 参数（A/B/C/D/none），返回值带 `health_grade`。

### 密码策略（注册、改密、重置均校验）

- 长度 8–128
- 至少包含「字母 / 数字 / 符号」中的**两类**
- 不得为单一重复字符（如 `aaaaaaaa`）
- 不得与用户名相同

### 可见性与分享

- `items.visibility`：`public`（默认，所有人可见）/ `private`（仅本人）
- `items.share_note`：公开时展示给其他用户的补充说明
- 同一件商品可由多个用户各自记录，`GET /api/public` 按 `name` 分组展示，并带 `owner_name`
- 被停用（`is_active=0`）或已进回收站（`deleted_at` 非空）的用户的记录不出现在公开广场
- 公开广场**分页对象是商品分组**，不是原始行——否则同一商品会被切到两页

### 用户生命周期

| 状态 | 接口 | 能否登录 | 数据 |
|---|---|---|---|
| 正常 | 注册 / `/api/users/restore` | 能 | 保留 |
| 停用 | `/api/users/active {active:false}` | 否（403） | 保留 |
| 回收站 | `/api/users/delete` | 否（401，按不存在处理） | **保留** |
| 已删除 | `/api/users/purge`（须先在回收站） | — | **真删** |

- 不能停用 / 降权 / 删除自己；至少保留一名未删除的管理员
- 未进回收站的账号调用 `/api/users/purge` 返回 400，防止一步误删


## 3. 接口明细

### GET /api/health
无需鉴权。存活探针，供 deploy.sh / nginx 使用。`version` 读自仓库根 `VERSION` 文件。
```
200 {"ok": true, "service": "shoppinglog-api", "version": "0.5.3"}
```

### GET /api/version
无需鉴权（nginx 里单独放行，不走 `auth_request`）。只返回版本号，供网页右下角徽标与
`deploy.sh` 判断「线上到底跑到哪了」。版本号唯一来源是仓库根 `VERSION` 文件。
```
200 {"ok": true, "service": "shoppinglog-api", "version": "0.5.3"}
```

### 商品与购买记录（0.6.2 起）

`GET /api/items` 返回的是**商品档案**，每条额外带购买历史：

| 字段 | 说明 |
|---|---|
| `purchases` | 购买记录数组（按日期倒序），元素含 `purchase_date / price / quantity / channel / place` 等 |
| `purchase_count` | 累计购买次数 |
| `total_spent` / `avg_price` / `min_price` / `max_price` | 累计花费 / 均价 / 最低 / 最高价 |
| `first_purchase_date` / `last_purchase_date` | 首次 / 最近购买日期 |

`POST /api/add` 两种用法：
1. **新建商品**：`name` + `price` 必填 → 建商品档案 + 首条购买记录。
2. **复购**：只传 `item_id` + 本次购买字段 → **只新增一条 `purchases`**，商品档案不重复建；
   未传的购买字段（渠道等）沿用上一次购买。返回 `{"ok": true, "id": <商品id>, "purchase_id": <购买id>, "reused": true}`。

`POST /api/update` 提交购买字段时，同步更新该商品**最近一次购买**（或 `purchase_id` 指定的那次）。
`POST /api/delete` 删除商品时级联删除其全部购买记录。
`POST /api/purchase/delete` 只删某一条购买记录（误记的这次），商品档案保留；
若还有剩余购买，商品上的「最近一次购买」缓存刷新为剩余里最新的那条。

### GET /api/auth-check
可选鉴权，用于前端判断登录态。
```
已登录: 200 {"ok": true, "user": "<username>"}
未登录: 401 {"ok": false}
```

### GET /api/me
返回当前用户脱敏信息。
```
200 {
  "ok": true,
  "id": 4, "username": "xmcioy", "display_name": "xmcioy",
  "is_admin": 1, "created_at": "..."
}
```

### GET /api/items
返回当前用户全部记录（含扩展表合并字段），按 `purchase_date DESC, id DESC` 排序。
- `images` / `nutrition` / `flavors` 已解析为 JSON；`tags` 已解析为数组。
```
200 {"ok": true, "items": [ {…记录字段…}, … ]}
```

### GET /api/settings
返回系统设置（仅 `allow_register`，模型相关键已全部移除）与用户设置（token 脱敏）。
```
200 {
  "ok": true,
  "system": {"allow_register": "0"},
  "user": {"token_masked": "…"}
}
```

### POST /api/register
开放注册受 `settings.allow_register` 控制（默认关闭）。
```
请求: {"username": "<2-32字符>", "password": "<≥6位>"}
200 {"ok": true, "username": "…", "token": "<新token>"}
400 缺参 / 长度不符；403 注册未开放；409 用户名已存在
```

### POST /api/login
登录成功即轮换 token（旧 token 失效），下发 Set-Cookie。
```
请求: {"username": "…", "password": "…"}
200 {"ok": true, "username": "…", "token": "<新token>"} + Set-Cookie: shopping_token=…
401 用户名或密码错误
```

### POST /api/logout
退出登录。只清 Cookie，**不作废 token**（同一 token 同时供 CLI / skill 使用）。

```
请求: {}   （带不带 token 都行）
200 {"ok": true}
Set-Cookie: shopping_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0
```

> 必须由服务端清：登录 Cookie 是 `HttpOnly` 的，前端 `document.cookie` 改不动它。

### POST /api/mcp
MCP（Model Context Protocol）端点，JSON-RPC 2.0。**MCP 就跑在这个服务进程里，不需要另起进程。**
鉴权与其它接口一致：`Authorization: Bearer <API Token>` 或 Cookie。

```
POST /api/mcp
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"1"}}}

200 {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"shoppinglog","version":"0.5.3"}}}
```

- 支持方法：`initialize` / `ping` / `tools/list` / `tools/call` / `resources/list` / `prompts/list`
- 通知类消息（无 `id`）返回 **202**，无响应体
- 支持批量（请求体为数组），逐条返回
- 未带有效 Token → 401（**必须是 401 JSON，不能被登录页 302 吞掉**）
- nginx 侧要点（见 `docs/nginx.shopping.conf.example`）：`/api/mcp` 要**单独放行、不走 `auth_request`**，
  并显式 `proxy_set_header Authorization $http_authorization`；否则 auth_request 子请求拿不到 Bearer，
  MCP 客户端会收到 `/login.html` 的 HTML 而不是 JSON-RPC 响应。
- 工具清单：`whoami` `list_items` `get_item` `add_item` `update_item` `delete_item` `upload_images` `public_feed` `stats`

> 工具内部通过回环调用本服务的 `/api/*`，所以权限、可见性、回收站等校验与网页**完全一致**。

### POST /api/add
新增记录（必填 `name`）。按 `category` 自动写入对应扩展表。
`nutrition / flavors / purchase_history / images` 既可以传 JSON 字符串，也可以直接传对象/数组（服务端会序列化）。
新增时会自动算健康评分并写入 `health_score / health_level / health_reason`（见「健康评分」一节）。
```
请求: {"name": "…", "purchase_date": "…", "category": "food", …任意公共/扩展字段…}
200 {"ok": true, "id": <新记录id>}
```

### POST /api/update
修改记录（必填 `id`），仅限本人记录；可更新公共字段，品类变更时自动迁移扩展表数据。
改了 `nutrition` 或 `ingredients` 时会**重算健康评分**（没传的那项取库里旧值）。
```
请求: {"id": 1, "name": "…", "category": "…", …}
200 {"ok": true, "updated": 1}
403 记录不存在或无权限
```

### POST /api/delete
删除记录（必填 `id`），级联删除扩展表与图片引用，仅限本人记录。
```
请求: {"id": 1}
200 {"ok": true, "deleted": 1}
403 记录不存在或无权限
```

### POST /api/purchase/delete
删除**单条购买记录**（必填 `id`，购买记录的 id，不是商品 id），仅限本人。
商品档案保留；若还有剩余购买记录，商品上的「最近一次购买」缓存刷新为剩余里最新的那条。
```
请求: {"id": 1}
200 {"ok": true, "deleted": 1, "item_id": 2, "purchase_count": true}
403 购买记录不存在或无权限
```

### GET /api/public
公开广场：所有 `visibility='public'` 的记录，**按商品名分组后分页**。**免登录**（nginx 层也用精确 location 放行）。

#### 只公开「商品 + 评价」，不公开「购买记录」

输出由 `PUBLIC_FIELDS` **白名单**裁剪，新加字段默认不公开：

| 会公开 | 不公开 |
|---|---|
| `name` `brand` `category` `subcategory` | `price` `original_price` `paid_price` `unit_price` `currency` |
| `rating` `review` `share_note` `repurchase` `tags` | `purchase_date` `place` `channel` `quantity` |
| `health_score` `health_level` `health_reason` `health_grade` | `purchase_history` `source` |
| `owner_name` `user_id` `product_group` `flavor_name` | **`produced_date` `expiry_date`**（属购买记录） |
| `images`（0.5.2 起公开，公开广场大图卡片用） | |
| `ingredients` `nutrition` `nutrition_analysis` `eating_method` | |
| `manufacturer` `manufacturer_address` `origin` `specs` `tips` `allergens` `storage_condition` `standard_no` `shelf_life` | |

分组里也**不返回** `last_date`（最近购买时间同样属于购买记录）。

> 0.5.2 起策略变化：**商品图片与成分信息改为公开**（公开广场按「小红书」式大图展示，
> 成分/营养/食用方式/其他信息默认折叠）。价格、购买日期、渠道、地点、数量、生产日期、到期日仍然一律不公开。
```
请求: GET /api/public?page=1&size=12&q=饼干[&flat=1][&grade=A]
200 {
  "ok": true, "flat": false, "grade": "A",
  "groups": [{"name": "同款饼干", "count": 2, "owner_count": 2, "last_date": "2026-10-01",
              "items": [{"owner_name": "…", "review": "…", …}, …]}, …],
  "items": […本页所有记录，扁平…],
  "total": 6, "page": 1, "size": 12, "pages": 1,
  "has_prev": false, "has_next": false
}
```
- `total` 是**商品种类数**（`COUNT(DISTINCT LOWER(TRIM(name)))`），不是记录条数
- `size` 上限 100，默认 12；`page` 越界返回空 `groups` 而不是报错
- `flat=1` 时按原始行分页（`items` 有值、`groups` 为空）
- `grade` 按**健康档次**筛选：`A`(≥80) / `B`(60–79) / `C`(40–59) / `D`(<40) / `none`（还没评分）；
  未知值返回 `400 {"ok": false, "error": "grade 只支持 A/B/C/D/none"}`

### GET /api/public-image
公开广场专用图片出口，**免登录**。为什么要单独一个接口：nginx 对 `\/images/*` 挂了登录鉴权
（图片属隐私数据），免登录的 `public.html` 直接 `<img src="/images/…">` 会被 302 成登录页 HTML，
浏览器表现为「图片一片空白」，很难排查。

``` 
请求: GET /api/public-image?p=images/2026-10/xxx.jpg
200  图片二进制（Content-Type 按扩展名，Cache-Control: public, max-age=86400）
403  {"ok": false, "error": "该图片未公开"}   —— 图片不在任何公开记录里
404  {"ok": false, "error": "图片不存在"}     —— 文件不存在 / 路径越界（非 images/ 前缀或含 ../）
```
- 放行条件：该路径必须**精确出现**在某条 `visibility='public'`、作者 `is_active=1` 且未在回收站的记录的 `images` 里
- 私有记录里的图、已删除用户留下的图，一律 403
- 路径经 `_abs_image()` 归一化，强制解析后仍位于 `ROOT/images` 下，防目录穿越

### POST /api/upload
多图本地 OCR 上传，触发本地识别与健康评分后写入记录。`images` 支持 **base64 数组**、**已归档路径数组**（`images/YYYY-MM/…`）或两者混合；已归档的直接用磁盘文件，不重复落盘。用户提交字段优先于 OCR 结果，识别不到自动留空。
```
请求: {"images": ["images/2026-10/a.jpg", "data:image/jpeg;base64,…"], "name": "用户已填优先", …}
200 {"ok": true, "id": <新记录id>, "ocr": {…识别字段…}, "health": {…评分…}}
400 缺参 / 图片无法解码
```

### POST /api/ocr
本地 OCR 预览（不保存）：逐张识别并给健康评分，供前端「识别图片并填充」使用。可传 `images`（base64）或 `paths`（已归档路径），或两者同时。
```
请求: {"paths": ["images/2026-10/a.jpg", …]}   或 {"images": ["data:image/jpeg;base64,…", …]}
200 {"ok": true, "fields": {…识别字段…}, "health": {…评分…}}
400 缺参 / 没有可识别的图片
```

### POST /api/images
图片预上传：把图片归档到 `images/YYYY-MM/` 并返回相对路径，**不创建记录**。表单第一步选图后即可调用，保存记录时再把路径作为 `images` 传给 `/api/add` 或 `/api/update`。
```
请求: {"images": ["data:image/jpeg;base64,…", …], "purchase_date": "2026-10-01", "name": "可选，用于文件名"}
200 {"ok": true, "images": ["images/2026-10/2026-10-01_xxx_1.jpg", …]}
400 缺参 / 单次超过 20 张 / 解码失败
```

### POST /api/images/delete
删除单张图片。带 `item_id` 时先从该记录的 `images` 数组里摘掉再删文件。
```
请求: {"path": "images/2026-10/a.jpg", "item_id": 12}
200 {"ok": true, "removed": 1, "images": ["images/2026-10/b.jpg"]}
400 路径不合法（只接受 images/ 前缀且不得越出 images 目录）
```

> `/api/update` 传入 `images` 时会与库中原值对比，**被移除的磁盘文件一并删除**，并返回 `images_removed` 数量。
> `/api/delete` 删除记录时也会清理其关联图片文件。

### POST /api/users/delete · restore · purge
管理员对用户的三级操作。默认（delete）是**软删除**，数据完整保留。
```
POST /api/users/delete  {"id": 5}   → 200 {"ok": true, "deleted": true}   移入回收站
POST /api/users/restore {"id": 5}   → 200 {"ok": true, "restored": true}  从回收站恢复
POST /api/users/purge   {"id": 5}   → 200 {"ok": true, "purged": true, "images_removed": 3}
400 不能删除自己 / 该用户已在回收站 / 不在回收站（purge 前置校验）/ 至少保留一名管理员
```

### POST /api/settings
更新设置：仅支持系统级 `allow_register`（管理员）；已移除全部模型相关键（base_url / name / api_key / model_token）。
```
请求: {"system": {"allow_register": "1"}}
200 {"ok": true, …脱敏回显…}
```
*（内容由AI生成，仅供参考）*
