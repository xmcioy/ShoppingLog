---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 5c462fda08d022c3bf06f0c7c13f492d_436d6215cf9111f1acb8525400638852
    ReservedCode1: iZW+9zkq9enjYsEebQjRaawt2WadnBOSKJzcvEkM/IBhSN5LZKVb3DTAPOGUndvC0DSzlF4mN22O0WJjn6sOAi3DmgHuX6Jdk1TjPqIdaX3coWbEyigctY5NgQa2gE3dFfNaSpykNB5oRzXPEa65LAlfodl+tiLjs/IOnh4t3/FMKiM5QcaZ0u5UGYk=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 5c462fda08d022c3bf06f0c7c13f492d_436d6215cf9111f1acb8525400638852
    ReservedCode2: iZW+9zkq9enjYsEebQjRaawt2WadnBOSKJzcvEkM/IBhSN5LZKVb3DTAPOGUndvC0DSzlF4mN22O0WJjn6sOAi3DmgHuX6Jdk1TjPqIdaX3coWbEyigctY5NgQa2gE3dFfNaSpykNB5oRzXPEa65LAlfodl+tiLjs/IOnh4t3/FMKiM5QcaZ0u5UGYk=
---

# ShoppingLog MySQL 部署手册（v0.2）

ShoppingLog 自 v0.2 起支持 SQLite / MySQL 双后端，默认 SQLite。本文说明如何切换到本地或远程 MySQL。

## 1. 前置条件

- MySQL 服务可用（本机示例端口 9306，默认端口 3306 亦可）。
- 需要 root（或具备建库建用户权限的账号）执行一次初始化。

## 2. 建专属库与受限用户（一次性）

以 root 登录执行：

```sql
CREATE DATABASE shoppinglog CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'shopping'@'localhost' IDENTIFIED BY '<强密码>';
CREATE USER 'shopping'@'127.0.0.1'  IDENTIFIED BY '<强密码>';

GRANT ALL PRIVILEGES ON shoppinglog.* TO 'shopping'@'localhost';
GRANT ALL PRIVILEGES ON shoppinglog.* TO 'shopping'@'127.0.0.1';
FLUSH PRIVILEGES;
```

> 安全原则：应用只使用 `shopping` 受限账号，仅对 `shoppinglog` 库有权限；root 密码只用于本次初始化，不写入任何配置文件。

命令行示例（本机 9306）：

```bash
mysql -h127.0.0.1 -P9306 -uroot -p -e "CREATE DATABASE shoppinglog CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -h127.0.0.1 -P9306 -uroot -p -e "CREATE USER 'shopping'@'localhost' IDENTIFIED BY '<强密码>'; CREATE USER 'shopping'@'127.0.0.1' IDENTIFIED BY '<强密码>'; GRANT ALL PRIVILEGES ON shoppinglog.* TO 'shopping'@'localhost'; GRANT ALL PRIVILEGES ON shoppinglog.* TO 'shopping'@'127.0.0.1'; FLUSH PRIVILEGES;"
```

## 3. 写本地配置文件

复制模板并填写：

```bash
cp config.example.json config.local.json
vim config.local.json
```

```json
{
  "db": {
    "engine": "mysql",
    "host": "127.0.0.1",
    "port": 9306,
    "user": "shopping",
    "password": "<强密码>",
    "database": "shoppinglog",
    "charset": "utf8mb4"
  }
}
```

- `config.local.json` 已被 `.gitignore` 排除，**不入库、不入仓库**。
- 引擎判定优先级：环境变量 `SHOPLOG_DB_ENGINE` > `config.local.json` 的 `db.engine` > 默认 `sqlite`。

## 4. 初始化与启动

```bash
# 建表（users/settings/items/品类扩展表，自动适配 MySQL 方言）
python3 scripts/migrate.py

# 启动 API 服务（自动读取 config.local.json）
mkdir -p logs
nohup python3 scripts/server.py --port 8787 >> logs/server.log 2>&1 &
```

验证：

```bash
curl http://127.0.0.1:8787/api/health
# 期望 {"ok": true}
```

## 5. 从 SQLite 迁移数据到 MySQL（可选）

1. 用 `docs/schema.sql` 在 MySQL 建好表（或直接跑 `migrate.py`）。
2. 编写一次性迁移脚本：用 sqlite3 读取 `data/shopping.db` 各表 → 通过 `scripts/db.py` 的 `db.connect()`（此时 config 指向 MySQL）逐行 `INSERT OR IGNORE`。
   - `users`：跳过 `admin`（本机已删除），保留真实用户与 Token；
   - `items`：保留原 `id` 与 `user_id`，保证扩展表外键一致；
   - 扩展表：先比对两端列集合，取交集插入（缺列用 `ALTER TABLE ... ADD COLUMN` 补齐，列定义参考 `scripts/shop.py` 的 `EXT_SCHEMAS`）。
3. 迁移后两端 `stats` 总额与记录数应一致。

> 本机已按此流程完成 SQLite → MySQL（shoppinglog 库，9306 端口）迁移并验证通过。

## 6. 切回 SQLite

删除或改掉 `config.local.json` 中的 `db.engine`（或设环境变量 `SHOPLOG_DB_ENGINE=sqlite`），重启服务即可；SQLite 数据不受影响。

## 7. 注意事项

- 代码层统一走 `scripts/db.py`：`PRAGMA table_info`、`sqlite_master`、`sqlite_sequence`、`datetime('now','localtime')`、`date('now', ?)`、`AUTOINCREMENT`、`INSERT OR IGNORE`、`ON CONFLICT` 等 SQLite 方言均自动转译。
- 建表语句 `docs/schema.sql` 为双引擎通用版：被索引/唯一/主键的文本列使用 `VARCHAR`，避免 MySQL `TEXT` 键限制；`settings.key` 因 MySQL 保留字改名为 `skey`。
- MySQL 8 以上默认 `utf8mb4`；本项目强制 `utf8mb4` 连接，支持中文与 emoji。
*（内容由AI生成，仅供参考）*
