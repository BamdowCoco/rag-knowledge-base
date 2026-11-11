# 公网部署（VPS + Caddy）

本目录提供公网部署的 compose 叠加配置和 Caddy 配置。完整中文步骤在
`docs/提升方案/云服务器部署.md`（本地文档，`docs/` 已被 gitignore）。

## 它做什么

```text
Internet 80/443
  -> Caddy（自动 TLS、Basic Auth、路径分区）
      /demo/*              -> rag-api，剥离前缀并注入 X-Demo-Mode: 1（访客区）
      /demo/admin* 等      -> 404
      /api/v1/documents*   -> api:8000（站主区）
      /api/v1/chunks*      -> api:8000（站主区）
      其余                 -> rag-api:8000（站主区：完整前端 + 检索 + 问答）
  mysql / redis / qdrant 只在 Docker 内网
```

访客区固定在 `/demo/`：

- 只能打开问答页 `/demo/` 和关于页 `/demo/about.html`；
- 问答范围由前端锁死、后端兜底：来源 `web`、召回数 `5`；`web` 语料就是
  FastAPI 中文文档，语言由语料保证；
- 运维页、摄入接口、`/metrics`、`/health`、`/docs` 在访客区直接 404 或 401。

站主区是根路径 `/`：完整前端、`/admin.html`、问答接口和摄入接口。

## 登录提示

- **站主**用根路径 `https://<PUBLIC_DOMAIN>/`，账号来自 `deploy/caddy/owner.caddy`；
- **访客**用 `https://<PUBLIC_DOMAIN>/demo/`，账号来自 `deploy/caddy/guests.caddy`；
- 访客账号不能进站主区，站主账号也不在 `/demo/` 的账号列表里（保持分区）；
- Basic Auth 输错用户名/密码时，浏览器会反复弹登录框，不会显示“密码错误”——这是浏览器
  接管 401 的行为，Caddy 无法自定义登录框里的报错；
- 弹窗的 realm 文本里带帮助地址；`https://<PUBLIC_DOMAIN>/demo/help.html` 免认证，
  说明账号来源、站主入口和 Basic Auth 的限制。

## 公网默认启动哪些服务

`compose.vps.yml` 把 `model` 与 `syncer` 归入 `mock` profile，公网部署默认只启动：

```text
api / rag-api / embedder / mysql / redis / qdrant / proxy
```

- 真实上游用 `crawler`（profile=crawler，手动运行）：

```text
docker compose -f docker-compose.yml -f deploy/compose.vps.yml \
  --profile crawler run --rm -e CRAWL_MAX_PAGES=150 crawler
```

- 需要在服务器上联调 mock 时：

```text
docker compose -f docker-compose.yml -f deploy/compose.vps.yml --profile mock up -d
```

- `.env` 保持 `ENRICH_PROVIDER=rule`（默认）或 `openai_compat`，不要用 `model`。

## VPS 快速开始

```bash
# 1. 域名 A 记录指向 VPS 公网 IP。
# 2. 安装 Docker Engine + compose plugin，防火墙只开 22/80/443。
# 3. 拉代码并配置：
cp .env.example .env
# 修改 PUBLIC_DOMAIN、DB_PASSWORD、LLM_API_KEY 等

# 4. 建账号文件（真实文件已被 gitignore）：
mkdir -p deploy/caddy
cp deploy/caddy/guests.example.caddy deploy/caddy/guests.caddy
bash deploy/guest_accounts.sh init-owner
# 记下打印出的站主密码；guests.caddy 里暂时保留默认 demo 账号

# 5. 在服务器上下载 embedding 模型：
bash scripts/fetch_embedding_model.sh

# 6. 启动整套服务：
docker compose -f docker-compose.yml -f deploy/compose.vps.yml up -d --build
docker compose ps
```

- 站主：浏览器打开 `https://<PUBLIC_DOMAIN>/`，用 `owner` 账号；
- 访客：打开 `https://<PUBLIC_DOMAIN>/demo/`，用 `deploy/guest_accounts.sh add` 生成的账号。

### 账号管理

```bash
# 新增一个岗位访客账号（写入 guests.caddy 并 reload）
bash deploy/guest_accounts.sh add

# 查看现有访客用户名（不显示密码）
bash deploy/guest_accounts.sh list

# 撤销某个岗位
bash deploy/guest_accounts.sh remove u1a2b3c4

# 删除默认演示账号（新账号验证通过后）
bash deploy/guest_accounts.sh remove demo
```

账号行格式与 hash 生成：

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext '你的密码'
```

```text
deploy/caddy/guests.caddy   访客账号：<用户名> <bcrypt hash>
deploy/caddy/owner.caddy    站主账号：<用户名> <bcrypt hash>
```

两个文件都在 `.gitignore` 里；仓库只提交 `*.example.caddy`。公司名和密码写本地的
`docs/投递/演示账号.md`，不要写进用户名或仓库。

访问日志（JSON，含 Basic Auth 的 `user_id`）：

```bash
docker exec rag-proxy tail -f /data/access.log
```

## Azure B2ls v2 + bamdow.kdns.fr

1. 在 kdns.fr 添加 A 记录：`bamdow` → Azure 公网 IP（当前 `20.249.208.224`）；
2. Azure NSG 入站放行 **22 / 80 / 443**；
3. 登录 VM，在仓库根目录跑：

```bash
bash deploy/preflight.sh
```

4. 4 GiB 内存先加 2 GB swap（见本地完整文档）；
5. 配好 `.env`（`PUBLIC_DOMAIN=bamdow.kdns.fr`）、账号文件和模型后，用同一条命令启动：

```bash
docker compose -f docker-compose.yml -f deploy/compose.vps.yml up -d --build
```

完整步骤、验收、备份、排错见本地 `docs/提升方案/Azure部署.md`。

## 没有域名 / 80/443 不可用时

先用 Cloudflare Quick Tunnel（VM 内 cloudflared + Caddy `:80`）拿临时 HTTPS 地址；
长期建议固定 Azure 公网 IP 并保留域名 + Caddy TLS。

## 安全边界

- Basic Auth 只能认证，不能做用户级授权；访客与站主靠两个账号列表 + 路径白名单隔离。
- 所有访客账号权限相同、共享只读数据；没有退出登录和细粒度审计，用户名只体现在 Caddy 访问日志里。
- 默认 `demo / rag-demo-2026` 已随公开仓库暴露，只作为过渡账号；新账号验证后立即
  `bash deploy/guest_accounts.sh remove demo`。
- 投递流程结束后撤销对应账号，避免长期可访问。
- 聊天接口没有限流；公开后被刷再考虑 Caddy 限流插件或 Cloudflare 规则。

## 安全清单

- 只暴露 22/80/443；MySQL/Redis/Qdrant 不映射公网。
- 使用强 `MYSQL_ROOT_PASSWORD` / `DB_PASSWORD`。
- `LLM_MAX_TOKENS` 不要开太大，盯运维页的 LLM 用量。
- 备份 MySQL（`mysqldump --single-transaction`）和 Qdrant snapshot。
- 没有 VPS 时，临时演示可用本地 Caddy + Cloudflare Tunnel（见本地完整文档）。
