# 公网部署（VPS + Caddy）

本目录提供公网部署的 compose 叠加配置和 Caddy 配置。完整中文步骤在
`docs/提升方案/云服务器部署.md`（本地文档，`docs/` 已被 gitignore）。

## 它做什么

```text
Internet 80/443
  -> Caddy（自动 TLS、路径分区、访客会话校验）
      /demo/login          -> 公开登录页
      /demo/api/v1/auth/*  -> 公开登录 / 退出 / forward_auth check
      /demo/*              -> forward_auth 后反代 rag-api（剥离前缀 + X-Demo-Mode）
      /demo/admin* 等      -> 404
      /api/v1/documents*   -> api:8000（站主区）
      /api/v1/chunks*      -> api:8000（站主区）
      其余                 -> rag-api:8000（站主区：完整前端 + 检索 + 问答）
  mysql / redis / qdrant 只在 Docker 内网
```

访客区固定在 `/demo/`：

- 打开 `/demo/` 会跳转到 `/demo/login`，登录后进入问答页；
- 只能打开问答页 `/demo/` 和关于页 `/demo/about.html`；
- 问答范围由前端锁死、后端兜底：来源 `web`、召回数 `5`；`web` 语料就是
  FastAPI 中文文档，语言由语料保证；
- 运维页、摄入接口、`/metrics`、`/health`、`/docs` 在访客区直接 404 或 401。

站主区是根路径 `/`：完整前端、`/admin.html`、问答接口和摄入接口，继续用 Basic Auth。

## 登录提示

- **站主**用根路径 `https://<PUBLIC_DOMAIN>/`，账号来自 `deploy/caddy/owner.caddy`，
  浏览器 Basic Auth 登录；
- **访客**打开 `https://<PUBLIC_DOMAIN>/demo/`，会跳到登录页，输入
  `deploy/caddy/guests.caddy` 里的用户名/密码；
- 访客密码错误会在登录页直接显示“用户名或密码错误”，不会再反复弹浏览器登录框；
- 登录成功后 Cookie 有效期 7 天，右上角“退出”可清 Cookie。

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
# 生成访客 Cookie 密钥并写入 DEMO_SESSION_SECRET：
openssl rand -hex 32

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
# 新增一个岗位访客账号（写入 guests.caddy，即时生效）
bash deploy/guest_accounts.sh add

# 查看现有访客用户名（不显示密码）
bash deploy/guest_accounts.sh list

# 撤销某个岗位（即时生效，旧 Cookie 下次校验就失效）
bash deploy/guest_accounts.sh remove u1a2b3c4

# 删除默认演示账号（新账号验证通过后）
bash deploy/guest_accounts.sh remove demo
```

账号行格式与 hash 生成：

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext '你的密码'
```

```text
deploy/caddy/guests.caddy   访客账号：<用户名> <bcrypt hash>（demo-auth 读取）
deploy/caddy/owner.caddy    站主账号：<用户名> <bcrypt hash>（Caddy Basic Auth）
```

两个文件都在 `.gitignore` 里；仓库只提交 `*.example.caddy`。公司名和密码写本地的
`docs/投递/演示账号.md`，不要写进用户名或仓库。

`compose.vps.yml` 会把 `deploy/caddy` 只读挂到 rag-api 的 `/etc/rag-demo`；
访客账号增删不需要 `caddy reload`，认证服务每次登录/校验都会重新读文件。
站主账号变更后仍需要 `caddy reload`。

访问日志：

```bash
# Caddy 访问日志：站主 Basic Auth 的 user_id 仍在这里
docker exec rag-proxy tail -f /data/access.log

# 访客登录成功/失败：看 rag-api 日志（含用户名和 IP，不含密码）
docker logs -f rag-knowledge-base-rag-api-1
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

- 站主用 Caddy Basic Auth；访客用表单登录 + HMAC 签名的 HttpOnly Cookie，两者互不相通。
- 访客 Cookie 不存密码，只存用户名 + 过期时间 + 签名；`Secure` / `SameSite=Lax` / `Path=/demo`。
- 所有访客账号权限相同、共享只读数据；没有角色/多租户，登录成功/失败日志在 rag-api。
- 登录失败限流 10 次 / 10 分钟（内存级，单 worker）；聊天接口本身仍没有限流。
- 默认 `demo / rag-demo-2026` 已随公开仓库暴露，只作为过渡账号；新账号验证后立即
  `bash deploy/guest_accounts.sh remove demo`。
- 投递流程结束后撤销对应账号；认证服务每次校验都读账号文件，旧 Cookie 立即失效。
- `DEMO_SESSION_SECRET` 必须配置；留空会使用进程内随机值，重启后所有访客会话失效。

## 安全清单

- 只暴露 22/80/443；MySQL/Redis/Qdrant 不映射公网。
- 使用强 `MYSQL_ROOT_PASSWORD` / `DB_PASSWORD`。
- `LLM_MAX_TOKENS` 不要开太大，盯运维页的 LLM 用量。
- 备份 MySQL（`mysqldump --single-transaction`）和 Qdrant snapshot。
- 没有 VPS 时，临时演示可用本地 Caddy + Cloudflare Tunnel（见本地完整文档）。
