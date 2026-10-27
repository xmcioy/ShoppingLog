---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 5c462fda08d022c3bf06f0c7c13f492d_e5212422cf9a11f1acb8525400638852
    ReservedCode1: 6JT0k82drm5utqkM+Y8hHMIoRY/oML/eZrojDMakqjV42JwhTjU9P1vZpIWEV6ShuDUz1KY+O9/maVyeQtSjTYZny0S9HfGYfoC3SwvAI7s1xQMx0kIVyOoyKfflz2suOaAl3PK/dHxUBtWeqiR4zZwqwzA2X6N1Ia6KoigEubelJRi1DGKrLPw1VhA=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 5c462fda08d022c3bf06f0c7c13f492d_e5212422cf9a11f1acb8525400638852
    ReservedCode2: 6JT0k82drm5utqkM+Y8hHMIoRY/oML/eZrojDMakqjV42JwhTjU9P1vZpIWEV6ShuDUz1KY+O9/maVyeQtSjTYZny0S9HfGYfoC3SwvAI7s1xQMx0kIVyOoyKfflz2suOaAl3PK/dHxUBtWeqiR4zZwqwzA2X6N1Ia6KoigEubelJRi1DGKrLPw1VhA=
---

# ShoppingLog 表结构设计（v0.2）

## 1. 设计原则

- **双引擎兼容**：同一套表结构同时支持 SQLite（默认）与 MySQL。主键均为自增 INTEGER，字段使用 SQLite/MySQL 通用的 `VARCHAR / INTEGER / REAL / TEXT` 类型。
- **幂等建表**：服务启动时 `ensure_schema()` 自动执行 `docs/schema.sql`（全部 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`），**用户无需手动建表**。
- **用户数据隔离**：主表 `items.user_id` 关联 `users.id`；所有查询按 `user_id` 过滤。
- **品类低耦合分表**：公共字段放主表 `items`，品类专属字段按 1:1 拆分到扩展表（`food_items` 等），避免单表列爆炸。扩展表以 `item_id` 为主键并级联删除。
- **文本结构化字段**：`images / nutrition / flavors` 以 JSON 文本存储，读取时解析为数组/对象；`tags` 以逗号分隔文本存储。

## 2. 表清单

| 表名 | 用途 | 关键约束 |
|---|---|---|
| `users` | 用户账号 | `username` 唯一；`token` 唯一索引 |
| `settings` | 系统级键值配置 | `skey` 主键 |
| `items` | 商品档案（名称/品牌/成分/评价/图片等） | `user_id` 索引；日期/品类索引 |
| `purchases` | 购买记录（0.6.0 起，一次购买一行） | `item_id` / (`user_id`,`purchase_date`) 索引 |
| `food_items` | 食品/饮品专属字段 | `item_id` 主键，外键级联删除 |
| `health_items` | 保健品专属字段 | 同上 |
| `beauty_items` | 美妆护肤专属字段 | 同上 |
| `clothing_items` | 服饰专属字段 | 同上 |
| `electronics_items` | 电子数码专属字段 | 同上 |

## 3. 核心表字段

### 3.1 users（用户表）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增主键 |
| username | VARCHAR(64) UNIQUE | 登录名，2-32 字符 |
| password_hash | VARCHAR(255) | PBKDF2-SHA256 哈希（`pbkdf2_sha256$salt$hash`），不存明文 |
| display_name | VARCHAR(64) | 展示名 |
| token | VARCHAR(64) UNIQUE | 登录令牌（Bearer / Cookie），登录时轮换 |
| is_admin | INTEGER | 1=管理员 |
| is_active | INTEGER | 1=启用（默认）；0=被管理员停用，此时 token 立即失效且无法登录 |
| deleted_at | VARCHAR(32) | 非空=已在回收站（软删除）。数据完整保留，token 立即失效、登录按「不存在」处理、不进公开广场 |
| created_at | TEXT | 创建时间 |

### 3.2 items（记录主表）

公共字段（列名即 JSON 字段名，前端直接透传）：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增主键 |
| user_id | INTEGER | 归属用户，隔离查询 |
| purchase_date | VARCHAR(10) | 购买日期 YYYY-MM-DD |
| category | VARCHAR(32) | 品类（决定扩展表路由） |
| subcategory | TEXT | 子分类 |
| name / brand | TEXT | 商品名 / 品牌 |
| price | REAL | 成交价（多件时=总价） |
| currency | VARCHAR(8) | 币种，默认 CNY |
| quantity | REAL | 数量，默认 1 |
| channel / place | TEXT | 渠道 / 购买地点 |
| rating | REAL | 评分 |
| original_price / paid_price | REAL | 原价 / 实付价 |
| gift_card_value / jd_bean_value | REAL | 礼品卡 / 京东豆抵扣 |
| unit_price | REAL | 单价（多件折算） |
| shelf_life / produced_date / expiry_date | TEXT | 保质期 / 生产日期 / 过期日期 |
| review | TEXT | 评价 |
| tags | TEXT | 逗号分隔标签 |
| repurchase | TEXT | 回购标记 |
| images | TEXT | JSON 数组：图片相对路径 |
| source | TEXT | 来源（手工/模型识别） |
| ingredients / nutrition / nutrition_analysis | TEXT | 配料 / 营养成分(JSON) / 分析 |
| manufacturer / manufacturer_address / manufacturer_phone | TEXT | 厂商信息 |
| tips / allergens / license | TEXT | 提示 / 过敏原 / 生产许可 |
| purchase_history | TEXT | 历史购买记录 |
| product_group / flavor_name | TEXT | 同产品分组 / 口味 |
| health_score / health_level / health_reason | REAL / TEXT / TEXT | 健康评分、等级、评分理由（后端 `health_score.py` 算好后存库） |
| visibility | VARCHAR(16) | `public`（默认，所有人可见）/ `private`（仅本人可见） |
| share_note | TEXT | 公开分享时的补充说明（他人可见） |
| created_at | TEXT | 创建时间 |

索引：`purchase_date`、`category`、`user_id`、`visibility`。

> **0.6.0 起语义调整**：`items` 表示「商品」，一次购买一行落在 `purchases`。
> `items` 上的 `price / purchase_date / quantity / channel / place / 各类价格列` 仍是
> 「最近一次购买」的冗余缓存（写入时与 `purchases` 同步），方便排序与统计；
> 历史价格、复购次数一律以 `purchases` 为准。

### 3.2.1 purchases（购买记录，一次购买一行）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增主键 |
| user_id | INTEGER | 归属用户（隔离查询） |
| item_id | INTEGER | 商品档案 `items.id` |
| purchase_date | VARCHAR(10) | 购买日期 YYYY-MM-DD |
| price | REAL | 成交价（多件时=总价） |
| currency | VARCHAR(8) | 币种，默认 CNY |
| quantity | REAL | 数量，默认 1 |
| unit_price | REAL | 单价（多件折算） |
| original_price / paid_price | REAL | 原价 / 实付价 |
| gift_card_value / jd_bean_value | REAL | 礼品卡 / 京东豆抵扣 |
| channel / place | TEXT | 渠道 / 购买地点 |
| note | TEXT | 本次购买备注 |
| created_at | TEXT | 录入时间 |

索引：`item_id`、`(user_id, purchase_date)`。删除商品时级联清理 `purchases`。

### 3.3 品类扩展表（1:1）

- `food_items`：食品专属（保质期、配料、营养成分、厂商、过敏原、执行标准、委托方、产地、储存条件、规格、口味等）。
- `health_items`：保健品专属（功效、用法用量、禁忌、不良反应、注意事项、批准文号等）。
- `beauty_items`：美妆专属（成分、规格、储存条件等）。
- `clothing_items`：服饰专属（尺码、颜色、材质、护理、风格、季节）。
- `electronics_items`：数码专属（型号、规格、保修、序列号）。

## 4. 建表方式

1. **推荐（自动）**：服务启动时 `ensure_schema()` 幂等执行 `docs/schema.sql`，表缺失即创建、已存在即跳过。
2. **手动**：
   - SQLite：`sqlite3 data/shopping.db < docs/schema.sql`
   - MySQL：`mysql -h<host> -P<port> -u<user> -p <database> < docs/schema.sql`

> 注意：MySQL 仅建表不建库/不建用户，库与用户需先就绪（见 `docs/MYSQL.md` 或 `bash scripts/deploy.sh --init-db`）。
*（内容由AI生成，仅供参考）*
