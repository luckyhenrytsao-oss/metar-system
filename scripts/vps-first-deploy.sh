#!/usr/bin/env bash
# ============================================================
# METAR 系统 —— VPS 首次手动部署脚本
# 用法：
#   1. 复制本脚本到 VPS（如 /root/vps-first-deploy.sh）
#   2. 修改下方 "必填变量"
#   3. chmod +x /root/vps-first-deploy.sh
#   4. ./root/vps-first-deploy.sh
# ============================================================
set -euo pipefail

# ---------------- 必填变量 ----------------
GHCR_USERNAME="luckyhenrytsao-oss"              # GitHub 用户名/组织名
GHCR_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # 需 write:packages 权限的 PAT
PROJECT_DIR="/opt/metar-system"                 # VPS 上项目目录
APP_PORT="8000"                                 # FastAPI 内部端口

# ---------------- 可选变量 ----------------
COMPOSE_FILE_URL="https://raw.githubusercontent.com/${GHCR_USERNAME}/metar-system/main/docker-compose.yml"
NGINX_CONF_URL="https://raw.githubusercontent.com/${GHCR_USERNAME}/metar-system/main/nginx/metar.conf"
USER_AGENT="MyMetarApp/1.0 (htsao2000@gmail.com)"

# ---------------- 1. 安装依赖 ----------------
echo "[1/7] 安装 Docker、Nginx..."
if command -v dnf &>/dev/null; then
    dnf install -y docker nginx curl
elif command -v apt-get &>/dev/null; then
    apt-get update
    apt-get install -y docker.io docker-compose nginx curl
else
    echo "不支持的包管理器，请手动安装 Docker 与 Nginx"
    exit 1
fi

# 启动 Docker
systemctl enable docker --now || true

# 确保 docker compose 可用
if ! docker compose version &>/dev/null; then
    echo "未检测到 docker compose 插件，请手动安装"
    exit 1
fi

# ---------------- 2. 创建项目目录 ----------------
echo "[2/7] 创建项目目录 ${PROJECT_DIR}..."
mkdir -p "${PROJECT_DIR}"
cd "${PROJECT_DIR}"

# ---------------- 3. 下载 docker-compose.yml ----------------
echo "[3/7] 下载 docker-compose.yml..."
curl -fsSL "${COMPOSE_FILE_URL}" -o docker-compose.yml

# ---------------- 4. 生成 .env ----------------
echo "[4/7] 生成 .env 配置文件..."
cat > .env <<EOF
REDIS_URL=redis://redis:6379/0
MONITOR_AIRPORTS=ZSPD,ZBAA,EGLC,RKSI,WSSS,LFPB,KSEA,KATL,KORD,KLGA,RJTT,KDAL,ZUUU,ZUCK,ZGGG,ZGSZ,ZHHH,ZSQD,EPWA,WMKK,RCSS,LLBG,RKPK,LIMC,KMIA,NZWN,KBKF,RPLL,CYYZ,SBGR,EDDM,KLAX,KAUS,EHAM,SAEZ,KSFO,LTAC,LTFM,FACT,VILK,OPKC,MPMG,KHOU,MMMX,OEJN,EFHK,LEMD,VHHH,UUWW
POLL_INTERVAL_SECONDS=1.5
METAR_TTL_SECONDS=7200
USER_AGENT=${USER_AGENT}
HTTP_TIMEOUT=15.0
LOG_LEVEL=INFO
EOF

# ---------------- 5. 登录 GHCR 并启动容器 ----------------
echo "[5/7] 登录 GHCR 并拉取镜像..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USERNAME}" --password-stdin

echo "启动容器..."
docker compose pull
docker compose up -d --remove-orphans

# ---------------- 6. 配置 Nginx ----------------
echo "[6/7] 配置 Nginx 反向代理..."
curl -fsSL "${NGINX_CONF_URL}" -o /etc/nginx/default.d/metar.conf

# 检查默认 server 块是否存在，若不存在则提示
if ! grep -q "include /etc/nginx/default.d/\*.conf;" /etc/nginx/nginx.conf; then
    echo "警告：/etc/nginx/nginx.conf 未 include default.d，请手动引入 Nginx 配置"
fi

nginx -t
systemctl enable nginx --now || true
systemctl restart nginx

# ---------------- 7. 健康检查 ----------------
echo "[7/7] 等待并执行健康检查..."
sleep 10

echo "--- 容器状态 ---"
docker compose ps

echo "--- /health ---"
curl -fsS "http://127.0.0.1:${APP_PORT}/health" && echo ""

echo "--- 示例 API (VHHH) ---"
curl -fsS "http://127.0.0.1:${APP_PORT}/api/v1/metar?icao=VHHH" | head -c 300 && echo ""

echo "--- Nginx 公网访问测试 ---"
PUBLIC_IP=$(curl -fsS -4 https://api.ipify.org || echo "127.0.0.1")
echo "公网 IP: ${PUBLIC_IP}"
curl -fsS "http://${PUBLIC_IP}/api/v1/metar?icao=VHHH" | head -c 200 && echo ""

echo ""
echo "✅ 首次部署完成！"
echo "后续 Push 到 main 分支将由 GitHub Actions 自动部署。"
