#!/usr/bin/env bash
# Azure / VPS 部署前只读检查。
# 用法（在服务器仓库根目录）：
#   bash deploy/preflight.sh
#
# 只读取信息，不安装、不修改任何东西。
set -u

DOMAIN="${PUBLIC_DOMAIN:-bamdow.kdns.fr}"
PUBLIC_IP="${PUBLIC_IP:-20.249.208.224}"

echo "===== Azure / VPS 部署前检查 ====="
echo "时间: $(date -Is 2>/dev/null || date)"
echo "内核: $(uname -sr)"
echo "架构: $(uname -m)"
echo "CPU : $(nproc 2>/dev/null || echo unknown)"
echo
echo "--- 内存 ---"
free -h 2>/dev/null || echo "free 不可用"
echo
echo "--- 磁盘 ---"
df -h / 2>/dev/null || echo "df 不可用"
echo
echo "--- Docker ---"
if command -v docker >/dev/null 2>&1; then
    docker --version
    docker compose version 2>/dev/null || echo "docker compose plugin 未安装"
else
    echo "Docker 未安装"
fi
echo
echo "--- sudo ---"
if sudo -n true 2>/dev/null; then
    echo "sudo 免密可用"
else
    echo "sudo 需要密码（或当前用户不在 sudoers）"
fi
echo
echo "--- 监听端口 ---"
ss -lnt 2>/dev/null | head -20 || echo "ss 不可用"
echo
echo "--- DNS ---"
echo "查询 $DOMAIN ..."
if getent hosts "$DOMAIN" 2>/dev/null; then
    echo "DNS 已解析"
else
    echo "警告：$DOMAIN 还没有解析到地址；请先在域名商添加 A 记录 -> $PUBLIC_IP"
fi
if getent hosts "$DOMAIN" 2>/dev/null | grep -q "$PUBLIC_IP"; then
    echo "OK：$DOMAIN 指向 $PUBLIC_IP"
else
    echo "注意：$DOMAIN 尚未指向 $PUBLIC_IP"
fi
echo
echo "--- 结论提示 ---"
echo "1) 内存 4GiB：建议加 2GB swap；低于 4GiB 不建议跑 MySQL+Qdrant+FastEmbed"
echo "2) 磁盘 30GB：构建 rag 镜像后注意 docker system df；不够就扩容"
echo "3) Azure NSG 需要放行 22/80/443（80/443 用于 Caddy TLS）"
echo "4) MySQL/Redis/Qdrant 的 3306/6379/6333 不应监听公网"
echo "=================================="
