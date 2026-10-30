# ShoppingLog

**Current version: v0.6.9**

Multi-user shopping log: what you bought, what it cost, and whether it's worth buying again.
Web / CLI / AI-client entry points, SQLite or MySQL backend, and all recognition & scoring happens
locally — no model API is ever called.

中文: [README.md](README.md)

![Architecture](docs/images/architecture.svg)

## Features

**Recording**
- Add / edit / delete from the browser; card and table views, search, filters, category and monthly charts
- **Photo entry**: drop order screenshots, packaging or nutrition labels → local OCR fills the fields →
  review and complete → save
- **Food health score**: rule-based 0–100 score (sugar / sodium / fat / fibre / additives) with a short reason
- Images are archived on selection and can be deleted one by one

**Products & purchases**
- Products and purchases are stored separately: buy the same thing N times and you still have one product
  record plus N purchase rows
- Repurchase count, total spent, average / min / max price, first and last purchase date are computed for you
- **Buy it again**: search an existing product in the dialog (or hit 🛒 on a card) and only fill in
  this purchase's price / date / quantity / channel

**Sharing**
- Every record is public or private; the public feed (`/public.html`) needs no login and groups reviews
  by product
- Only product info, images, ingredients and reviews are public — **prices, dates and channels never are**

**Accounts & integrations**
- Per-user data isolation; password policy, password change, self-service token reset; admin back office
  and user recycle bin
- MCP server runs inside the service (`POST /api/mcp`, 9 tools); AI clients authenticate with an API token
  and never touch the database
- `scripts/shop.py` CLI uses the same token

## Install

```bash
# 1. clone
git clone <repo-url> ShoppingLog && cd ShoppingLog

# 2. create the database (SQLite by default; users/settings/items/purchases/category tables + admin)
python3 -m venv .venv && source .venv/bin/activate
python3 scripts/migrate.py

# 3. start the API service (127.0.0.1:8787 only)
mkdir -p logs
nohup python3 scripts/server.py --port 8787 >> logs/server.log 2>&1 &

# 4. expose it with nginx (see docs/nginx.shopping.conf.example)
#    listening on 8088: static pages + /api/* proxy; /api/mcp passes Authorization through

# 5. autostart (optional)
bash scripts/install-service.sh
```

Open `http://localhost:8088`. On first deploy an admin account is created and written to
`data/INITIAL_ADMIN.txt` — change the password and delete that file right after login.
The public feed needs no login: `http://localhost:8088/public.html`.

For MySQL set `db.engine = "mysql"` in `config.local.json`; see [docs/MYSQL.md](docs/MYSQL.md).

## Photo entry flow

![Photo entry flow](docs/images/upload-flow.svg)

1. Hit **＋ New record** → pick or drop images (multi-select). They are archived and recognised locally at once
2. The form opens with recognised fields highlighted; fill in what's missing (name and price are required)
3. Save. Buying something you already own? Search it above the form and use **Buy again** instead

## Documentation

| Doc | Contents |
|---|---|
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Release notes |
| [docs/api.md](docs/api.md) | HTTP API & MCP reference |
| [docs/TESTING.md](docs/TESTING.md) | Tests and deployment acceptance |
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture & program design |
| [docs/schema.md](docs/schema.md) · [docs/schema.sql](docs/schema.sql) | Schema design & DDL |
| [docs/MYSQL.md](docs/MYSQL.md) | MySQL deployment & migration |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What's next |
| [docs/nginx.shopping.conf.example](docs/nginx.shopping.conf.example) | nginx example config |
