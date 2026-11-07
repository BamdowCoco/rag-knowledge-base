# 公网部署（VPS + Caddy）

本目录提供公网部署的 compose 叠加配置和 Caddy 配置。完整中文步骤在
`docs/提升方案/云服务器部署.md`（本地文档，`docs/` 已被 gitignore）。

## 它做什么

```text
Internet 80/443
  -> Caddy（自动 TLS、Basic Auth）
      /api/v1/documents*  -> api:8000
      /api/v1/chunks*     -> api:8000
      其余                -> rag-api:8000（前端 + 检索 + 问答）
  mysql / redis / qdrant 只在 Docker 内网
```

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

# 4. 在服务器上下载 embedding 模型：
bash scripts/fetch_embedding_model.sh

# 5. 启动整套服务：
docker compose -f docker-compose.yml -f deploy/compose.vps.yml up -d --build
docker compose ps
```

浏览器打开 `https://<PUBLIC_DOMAIN>/`。

## Azure B2ls v2 + bamdow.kdns.fr

1. 在 kdns.fr 添加 A 记录：`bamdow` → Azure 公网 IP（当前 `20.249.208.224`）；
2. Azure NSG 入站放行 **22 / 80 / 443**；
3. 登录 VM，在仓库根目录跑：

```bash
bash deploy/preflight.sh
```

4. 4 GiB 内存先加 2 GB swap（见本地完整文档）；
5. 配好 `.env`（`PUBLIC_DOMAIN=bamdow.kdns.fr`）和模型后，用同一条命令启动：

```bash
docker compose -f docker-compose.yml -f deploy/compose.vps.yml up -d --build
```

完整步骤、验收、备份、排错见本地 `docs/提升方案/Azure部署.md`。

## 没有域名 / 80/443 不可用时

先用 Cloudflare Quick Tunnel（VM 内 cloudflared + Caddy `:80`）拿临时 HTTPS 地址；
长期建议固定 Azure 公网 IP 并保留域名 + Caddy TLS。

## Basic Auth

`deploy/Caddyfile.vps` 里带了演示账号：

- 用户名：`demo`
- 密码：`rag-demo-2026`

正式暴露前必须改：

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'your-password'
```

把 `Caddyfile.vps` 里的 hash 替换掉，然后重启 `proxy`。

## 安全清单

- 只暴露 22/80/443；MySQL/Redis/Qdrant 不映射公网。
- 使用强 `MYSQL_ROOT_PASSWORD` / `DB_PASSWORD`。
- `LLM_MAX_TOKENS` 不要开太大，盯运维页的 LLM 用量。
- 备份 MySQL（`mysqldump --single-transaction`）和 Qdrant snapshot。
- 没有 VPS 时，临时演示可用本地 Caddy + Cloudflare Tunnel（见本地完整文档）。
