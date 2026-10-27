# ShoppingLog 知识架构

> 生成日期：2026-10-24
> 代码基线：`aa2f787` + 工作区未提交改动（含 `local_ocr.py` / `health_score.py` / `ocr_vision.swift` 三个未跟踪新文件）
> 性质：只读检查产出，未修改任何项目文件

---

## 1. 项目一句话定位

多用户购物记录服务。用户在网页或 CLI 录入商品（含图片），系统用**本地 OCR + 纯规则算法**自动结构化字段并给出食品健康评分，数据按用户隔离存储，可在 SQLite / MySQL 之间切换。

---

## 2. 四层知识结构

```
┌─────────────────────────────────────────────────────────────┐
│ L4 表现层  site/index.html  login.html  register.html      │
│            纯原生 JS，无构建步骤；卡片/tab/搜索/图表/弹窗    │
│            dashboard.html ← scripts/template.html（静态快照） │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ L3 服务层  scripts/server.py    HTTP API（127.0.0.1:8787）   │
│            scripts/shop.py      CLI + 被 server 复用的领域逻辑│
│            nginx :8088          反代 + auth_request 统一鉴权   │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ L2 智能层  scripts/local_ocr.py     字段解析（纯规则）       │
│            scripts/ocr_vision.swift macOS Vision OCR        │
│            scripts/health_score.py  食品健康评分（纯规则）    │
│            scripts/model_client.py  ← 已成死代码，见 §5       │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ L1 数据层  scripts/db.py      SQLite/MySQL 统一方言适配层     │
│            docs/schema.sql    权威建表语句                  │
│            users / settings / items + 5 张品类扩展表         │
└─────────────────────────────────────────────────────────────┘
        横向贯穿：scripts/migrate.py（幂等迁移）
                  skill/shopping-log/SKILL.md（AI 客户端技能）
                  scripts/deploy.sh · run.sh（部署）
```

---

## 3. 关键知识节点

### 3.1 数据模型：`items` 主表 + 5 张品类扩展表

| 表 | 职责 | 关键约束 |
|---|---|---|
| `users` | 账号 | `username` 唯一；`token` 唯一索引；`password_hash` 为 pbkdf2_sha256 |
| `settings` | 系统级键值 | 主键 `skey`（非 `key`，`key` 是 MySQL 保留字） |
| `items` | 记录主表（公共字段） | `user_id` 索引；`purchase_date`/`category` 索引 |
| `food_items` | 食品餐饮 | `item_id` 主键 + 级联删除 |
| `health_items` | 药品保健 | 同上 |
| `beauty_items` | 美妆护肤 | 同上 |
| `clothing_items` | 服饰鞋包母婴运动 | 同上 |
| `electronics_items` | 数码家电 | 同上 |

**归属判据只有一处**：`items.user_id`。扩展表不冗余 `user_id`，靠 `item_id` 1:1 关联。数据隔离全部落在 `WHERE user_id = ?`。

**品类路由表**（`scripts/shop.py` 的 `CAT_TO_EXT`）：食物/食品/餐饮→food；药品保健→health；美妆护肤→beauty；衣服/鞋/包配/运动户外/母婴→clothing；数码家电→electronics；其余品类只走主表。

### 3.2 双后端适配层（`scripts/db.py`）

- 引擎选择优先级：`SHOPLOG_DB_ENGINE` 环境变量 > `config.local.json` 的 `db.engine` > 默认 `sqlite`
- 对外只暴露 `connect()`，返回统一 `Conn`，接口面兼容 `sqlite3.Connection`
- `CompatRow` 兼容 `sqlite3.Row`：支持 `row["col"]` / `row[0]` / `keys()` / `dict(row)`
- 方言转译靠正则：占位符 `?`→`%s`、`AUTOINCREMENT`→`AUTO_INCREMENT`、`INSERT OR IGNORE`→`INSERT IGNORE`、`ON CONFLICT`→`ON DUPLICATE KEY UPDATE`、`sqlite_master`→`information_schema`、`PRAGMA table_info`→`information_schema.columns`
- 特殊处理：`CREATE INDEX IF NOT EXISTS`（MySQL 不支持）改为先查存在性再建

### 3.3 本地 OCR 识别链（替代原 AI 模型方案）

```
浏览器选图 → base64 → POST /api/ocr 或 /api/upload
   ↓
server.py 落临时文件 → local_ocr.ocr_fields()
   ↓
ocr_bin(): 定位 ~/myapp/shoppinglog/bin/ocr_vision
           └─ 不存在则 swiftc -O 现场编译 ocr_vision.swift
   ↓
ocr_vision.swift: VNRecognizeTextRequest（zh-Hans/en-US）
           按 midY 降序 + minX 升序 → 阅读顺序文本
   ↓
parse_ocr_text(): 正则抽字段
   品名/品牌/价格/净含量/生产日期/保质期至/保质期/配料表/营养成分
   ↓
_guess_category(): 命中食品特征词 → "食物"，否则 "其他"
   ↓
health_score.score_food(): 纯规则算 0-100 分
   ↓
写库：用户显式字段 > OCR 结果；图片归档 images/YYYY-MM/
```

**规则解析的关键约定**：
- 生产日期与到期日是**两个独立字段**，缺一不可
- 配料表切分点：`储存|贮存|食用方法|致敏|过敏原`
- 营养成分按每 100g 基准，标准键：`energy_kj/protein_g/fat_g/sat_fat_g/carbs_g/sugar_g/fiber_g/sodium_mg`
- 分类只猜「食物 / 其他」二值，不做细分类

### 3.4 健康评分算法（`scripts/health_score.py`）

基线 70 分加减：
- 减分：能量≥2000kJ(-10)、脂肪>20g(-12)、糖>15g(-12)、钠>800mg(-12)、饱和脂肪≥5g(-8)、碳水>60g(-4)
- 加分：蛋白质≥10g(+8)、膳食纤维≥5g(+10)
- 添加剂：0 种(+5)、1 种(0)、2-4 种(-5)、>4 种(-10)
- 风险配料单独扣分（氢化植物油/起酥油/人造奶油/植脂末 各 -10），总扣分封顶 -18
- 分档：≥85 优秀 / ≥70 良好 / ≥55 一般 / 其余需注意；分数夹在 5-99

### 3.5 双录入通道

| 通道 | 入口 | 数据路径 | 隔离方式 |
|---|---|---|---|
| CLI | `shop.py add/list/...` | 本机直连 DB | `--token` → `user_id` 过滤 |
| CLI 远端 | `shop.py --api URL --token T` | HTTP 转发 | `Authorization: Bearer` |
| 网页 | `site/index.html` | `fetch /api/*` | Cookie `shopping_token`（HttpOnly, 180 天） |
| AI 技能 | `skill/shopping-log/SKILL.md` | 调 `shop.py add` + `sync_site.py` | 同 CLI |

**登录即轮换 token**：每次登录生成新 token 并覆盖旧值，旧 token 立即失效。

### 3.6 部署分离模型

- 源码：`$HOME/project/ShoppingLog`（git 仓库，只放代码文档）
- 运行数据：`$HOME/myapp/shoppinglog`（config / data / images / exports / logs，升级不触碰）
- 环境变量 `SHOPLOG_ROOT` 指向运行目录；`SHOPLOG_APP` 指向 OCR 二进制目录
- `site/images` 是指向 `$APP/images` 的软链，不提交
- `scripts/deploy.sh --install` 首次部署 / 默认升级（拉取+重启+健康检查）

---

## 4. 已确认的运行现状

- 线上服务在跑：`127.0.0.1:8787` 返回 `{"ok":true,...,"version":"0.2.0"}`，nginx 监听 8088
- 实际后端是 **MySQL**（`config.local.json` engine=mysql，端口 9306），非默认 SQLite
- 实际解释器是 WorkBuddy 内置 **Python 3.11**（`MarvisAgent/.../python311`），**系统 `python3` 与托管 3.13 均无 `pymysql`**
- `~/myapp/shoppinglog/` 下**没有 `docs/` 目录**
- 仓库有 4 个 remote，其中 2 个 URL 内嵌明文凭据（GitHub `gho_` token、Gitea token）

---

## 5. 已核实的缺陷（按影响排序）

### 🔴 P0-1：`dashboard.html` 把 15 条真实消费记录提交进了公开仓库

- `dashboard.html` 被 git 跟踪，且 `.gitignore` **未**覆盖它（`git check-ignore` 判定为"会被提交"）
- 文件内含 15 条真实记录：商品名、价格、购买日期、评分、品牌、渠道
- **已进入历史提交**（`0a27d8e`、`99f25bc`、`b9b98e0`），并已推送到 `github/main` 与 `gitea/main`
- `.gitignore` 保护了 `site/data.js`、`*.db`、`images/`，却漏了 `dashboard.html` —— 后者由 `shop.py report` 从数据库生成，性质完全相同
- 仓库目标为 GitHub **公开**仓库（README 明确写"GitHub 公开推送"）
- 修复需要：加 `.gitignore` 规则 + 从历史中清除 + 轮换已泄露的 token

### 🔴 P0-2：网页编辑会清空品类型扩展字段（实测）

`_handle_update`（`scripts/server.py:424-432`）在收到 `category` 时先 `DELETE FROM {target_ext}`，再只用请求体里**存在的**字段重建。前端 `submitItem` 只提交 `base`（name/price/category/brand/channel/quantity/purchase_date/review/expiry_date/shelf_life/ingredients），**不回传也不回填** manufacturer/license/origin/storage_condition/allergens/flavors/nutrition 等。

实测结果（全新库，食品类记录，编辑一次改名）：
```
编辑前：nutrition, manufacturer, allergens, license, origin, storage_condition, flavors
编辑后：仅剩 shelf_life, expiry_date, ingredients（均为空串）
清空字段：['nutrition','manufacturer','allergens','license','origin','storage_condition','flavors']
```
数据在编辑时静默丢失，无报错。CLI 的 `cmd_update` 有同样模式（同样先删后插），同样风险。

### 🟠 P1-1：全新部署无法启动

`ensure_schema()`（`scripts/server.py:64-88`）依赖 `docs/schema.sql`，但路径是 `SHOPLOG_ROOT/docs/schema.sql` —— 指向**运行目录**，而 `docs/` 只存在于源码目录。

实测：空运行目录启动 `server.py` → `sqlite3.OperationalError: no such table: items`，进程直接退出。原因是 `SCHEMA_SQL.exists()` 为 False 跳过了建表，随即 `ALTER TABLE items ADD COLUMN user_id` 打在不存在的表上。

线上没暴露此问题，因为 `~/myapp/shoppinglog/` 虽无 `docs/`，但 MySQL 库里的表是早前手工建好的。

### 🟠 P1-2：`migrate.py` 同样路径错误 + 目录未创建

`migrate.py` 的 `ROOT` 硬编码为 `Path(__file__).parent.parent`（源码目录），**不读 `SHOPLOG_ROOT`**；而 `db.py` 读 `SHOPLOG_ROOT`。两者指向不同库。实测分离部署下 `migrate.py` 报 `unable to open database file`（`$APP/data/` 尚未创建，而 `db.py` 不会自动建目录）。`server.py` 有 `DB.parent.mkdir`，`migrate.py` 没有。

### 🟠 P1-3：MySQL 模式下 `shop.py` 自建表缺关键列

`shop.py` 内置 `SCHEMA` 常量与 `docs/schema.sql` 已经分叉。差集（脚本比对结果）：
- `schema.sql` 有、`shop.py` SCHEMA 无：`user_id`、`health_score`、`health_level`、`health_reason`、`purchase_history`、`product_group`、`flavor_name`
- 其中 `purchase_history`/`product_group`/`flavor_name` 靠 `EXTRA_COLUMNS` 补回
- **补不回来、仍然缺**：`user_id`、`health_score`、`health_level`、`health_reason`

`user_id` 缺失直接导致「表 items has no column named user_id」。凡是走 `shop.connect()` 而非 `schema.sql` 的建库路径（`migrate.py` 的 else 分支、CLI 直接跑），都会拿到缺列的表。

### 🟡 P2-1：前端评分与后端评分是**两套不同算法**

- 后端 `health_score.py`：基线 70 分加减制，看添加剂与风险配料
- 前端 `site/index.html:377` `healthScore()`：NRV 百分比扣分制（A/B/C/D 四级）
- 卡片渲染 `cardBodyHTML` 用的是**前端那套**（`healthScore(it.nutrition)`），完全没用后端存进库的 `health_score`/`health_level` 字段
- 结果：同一份数据，OCR 弹窗提示一个分数、卡片显示另一个分数
- 且 `healthGrade` 的 A/B/C/D 与后端「优秀/良好/一般/需注意」分级也不一致

### 🟡 P2-2：`model_client.py` 已是死代码

`server.py` 已完全改走本地 OCR，`model_client.py` 无任何调用点（仅自身注释与 `__pycache__` 残留引用）。但 `README.en.md` 直到用户本轮更新前仍在目录结构里列它，`docs/DESIGN.md` §4.1 仍描述「图文上传 → 调 model_client.py」，`docs/api.md` 仍写 `/api/upload` 是 multipart 模型识别 —— 三处文档与实现不符。

### 🟡 P2-3：设置面板字段与服务端能力脱节

前端 `openSettings()`/`saveSettings()` 仍读写 `model_base_url`/`model_name`/`model_api_key`/`model_token`；后端 `_handle_settings_get` 只返回 `allow_register`，`_handle_settings_post` 只处理 `allow_register`。用户填模型配置 → 静默丢弃，无提示。

### 🟡 P2-4：远端 URL 内嵌明文凭据

`git remote -v` 显示 `github` 与 `gitea` 两个 remote 的 URL 中直接写有 `gho_` 开头的 GitHub token 和 Gitea token。凭据出现在 `.git/config`（不在提交历史里，但会随配置备份/克隆泄露）。

---

## 6. 未核实事项

- MySQL 生产库的实际表结构与列（`pymysql` 仅存在于 WorkBuddy 内置 3.11 解释器，本轮未直连读取，避免对生产库产生写操作；上表列结构依据 `docs/schema.sql` 与代码）
- `swiftc` 现场编译 `ocr_vision.swift` 是否在当前环境成功（`~/myapp/shoppinglog/bin/ocr_vision` 已存在，但未验证其新鲜度与识别质量）
- nginx 实际生效配置与 `site/images` 软链是否正常
- 腾讯文档同步链路（`SKILL.md` 第 7 步要求同步到腾讯文档，本轮未见对应脚本）

---

## 7. 修复进度（截至 2026-10-24 20:35）

| # | 动作 | 状态 | 涉及文件 |
|---|---|---|---|
| 1 | 移除 `dashboard.html` 跟踪 + 加 `.gitignore` + 清理历史 | ✅ 已完成（本地） | `.gitignore`、git 历史 |
| 2 | 修 `_handle_update` 扩展字段丢失（增量更新 + 前端回填） | ✅ 已完成 | `scripts/server.py`、`scripts/shop.py`、`site/index.html` |
| 3 | 统一 `ROOT` 解析为 `SHOPLOG_ROOT`，`migrate.py` 补 `mkdir` | ✅ 已完成 | `scripts/migrate.py`、`scripts/server.py` |
| 4 | `shop.py` 的 SCHEMA 收敛到 `docs/schema.sql`（单一权威源） | ✅ 已完成 | `scripts/shop.py` |
| 5 | 统一健康评分：后端算好存库，前端只展示 | ✅ 已完成 | `site/index.html` |
| 6 | 清理 `model_client.py` 死代码 + 同步文档 | ✅ 已完成 | `docs/DESIGN.md`、`docs/api.md` |
| 7 | 远端 URL 去掉内嵌凭据，改用 credential helper | ⚠️ 本地已完成 | `.git/config`（平台侧 token 轮换待人工） |

**仍需人工完成**：
- ⚠️ 到 GitHub / Gitea 平台侧吊销并重新生成 token（旧 token 曾明文存在，按已泄露处理）
- ⚠️ `git push --force` 推送清理后的历史（远端目前仍是含泄露数据的旧历史）

> 详细修复记录与验证证据见本地协作文档（`docs/coordination/`，仅本地分发，不进仓库）。

