-- ShoppingLog 0.2 完整建表语句
-- SQLite 可直接执行：sqlite3 data/shopping.db < docs/schema.sql
-- MySQL 可直接执行：mysql -h127.0.0.1 -P9306 -ushopping -p shoppinglog < docs/schema.sql
-- 推荐方式：运行 scripts/migrate.py（幂等，自动适配双引擎）

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    display_name  VARCHAR(64),
    token         VARCHAR(64) NOT NULL UNIQUE,
    is_admin      INTEGER DEFAULT 0,
    is_active     INTEGER DEFAULT 1,          -- 1=启用，0=被管理员停用（停用后无法登录）
    deleted_at    VARCHAR(32),                -- 非空=已在回收站（软删除，数据保留，无法登录）
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_token ON users(token);
CREATE INDEX IF NOT EXISTS idx_users_deleted ON users(deleted_at);

-- 系统级设置（键值）
CREATE TABLE IF NOT EXISTS settings (
    skey  VARCHAR(64) PRIMARY KEY,
    value TEXT
);

-- 记录主表（公共字段）
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER,
    purchase_date VARCHAR(10) NOT NULL,
    category      VARCHAR(32),
    subcategory   TEXT,
    name          TEXT NOT NULL,
    brand         TEXT,
    price         REAL,
    currency      VARCHAR(8) DEFAULT 'CNY',
    quantity      REAL DEFAULT 1,
    channel       TEXT,
    place         TEXT,
    rating        REAL,
    original_price REAL,
    paid_price     REAL,
    gift_card_value REAL,
    jd_bean_value  REAL,
    unit_price    REAL,
    shelf_life    TEXT,
    produced_date TEXT,
    expiry_date   TEXT,
    review        TEXT,
    tags          TEXT,
    repurchase    TEXT,
    images        TEXT,
    source        TEXT,
    ingredients   TEXT,
    nutrition     TEXT,
    nutrition_analysis TEXT,
    manufacturer      TEXT,
    manufacturer_address TEXT,
    manufacturer_phone   TEXT,
    tips              TEXT,
    allergens         TEXT,
    license           TEXT,
    purchase_history TEXT,
    product_group    TEXT,
    flavor_name      TEXT,
    created_at    TEXT,
    health_score REAL,
    health_level TEXT,
    health_reason TEXT,
    visibility    VARCHAR(16) DEFAULT 'public',  -- public=对他人可见（默认），private=仅本人可见
    share_note    TEXT                            -- 分享时的补充说明（他人可见）
);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(purchase_date);
CREATE INDEX IF NOT EXISTS idx_items_cat  ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id);
CREATE INDEX IF NOT EXISTS idx_items_vis  ON items(visibility);

-- 购买记录表（0.6.0 起）：一次购买一行，与商品档案 items 1:N
-- 设计取舍：items 存「商品」（名称/品牌/成分/评价/图片），purchases 存「每一次购买」
-- （日期/价格/数量/渠道）。同一件商品买 N 次就有 N 行，价格历史与复购统计直接可算，
-- 商品信息不再被重复 N 份而互相打架。items 上保留的 price/purchase_date 等列只是
-- 「最近一次购买」的冗余缓存（由 purchases 派生），不另做权威来源。
CREATE TABLE IF NOT EXISTS purchases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    item_id         INTEGER,                     -- 商品档案 items.id
    purchase_date   VARCHAR(10) NOT NULL,
    price           REAL,
    currency        VARCHAR(8) DEFAULT 'CNY',
    quantity        REAL DEFAULT 1,
    unit_price      REAL,
    original_price  REAL,
    paid_price      REAL,
    gift_card_value REAL,
    jd_bean_value   REAL,
    channel         TEXT,
    place           TEXT,
    note            TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_item ON purchases(item_id);
CREATE INDEX IF NOT EXISTS idx_purchases_user_date ON purchases(user_id, purchase_date);

-- 品类扩展表（低耦合分表：item_id 关联主表 1:1；列定义与 shop.EXT_SCHEMAS 保持一致）
CREATE TABLE IF NOT EXISTS food_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    shelf_life TEXT, produced_date TEXT, expiry_date TEXT,
    eating_method TEXT, image_desc TEXT, ingredients TEXT,
    nutrition TEXT, nutrition_analysis TEXT, manufacturer TEXT,
    manufacturer_address TEXT, manufacturer_phone TEXT,
    tips TEXT, allergens TEXT, license TEXT, standard_no TEXT,
    entrusting_party TEXT, entrusting_address TEXT, origin TEXT,
    storage_condition TEXT, service_hotline TEXT, specs TEXT, flavors TEXT
);
CREATE TABLE IF NOT EXISTS health_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    shelf_life TEXT, produced_date TEXT, expiry_date TEXT,
    manufacturer TEXT, manufacturer_address TEXT, manufacturer_phone TEXT,
    tips TEXT, license TEXT, origin TEXT, storage_condition TEXT,
    service_hotline TEXT, specs TEXT, indications TEXT, dosage TEXT,
    contraindications TEXT, adverse_reactions TEXT, cautions TEXT,
    approval_no TEXT
);
CREATE TABLE IF NOT EXISTS beauty_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    shelf_life TEXT, produced_date TEXT, expiry_date TEXT,
    ingredients TEXT, manufacturer TEXT, manufacturer_address TEXT,
    manufacturer_phone TEXT, tips TEXT, license TEXT, origin TEXT,
    storage_condition TEXT, service_hotline TEXT, specs TEXT,
    image_desc TEXT
);
CREATE TABLE IF NOT EXISTS clothing_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    size TEXT, color TEXT, material TEXT, care TEXT,
    style TEXT, season TEXT
);
CREATE TABLE IF NOT EXISTS electronics_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    model TEXT, specs TEXT, warranty TEXT, serial TEXT
);
