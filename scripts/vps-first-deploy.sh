#!/usr/bin/env bash
# VPS 首次手动部署脚本
# 用法：将本文件传到 VPS 后执行 bash scripts/vps-first-deploy.sh
# 或逐行复制到 SSH 会话中执行。

set -euo pipefail

PROJECT_DIR="/opt/metar-system"
GITHUB_OWNER="luckyhenrytsao-oss"
REPO_NAME="metar-system"
IMAGE="ghcr.io/${GITHUB_OWNER}/${REPO_NAME}:latest"

echo "=========================================="
echo "METAR System VPS 首次部署"
echo "=========================================="

# 1. 安装 Docker（如未安装）
if ! command -v docker &> /dev/null; then
    echo "[1/7] 安装 Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    echo "[1/7] Docker 已安装，跳过"
fi

# 2. 登录 GitHub Container Registry
echo "[2/7] 登录 GHCR..."
echo "请在下一步粘贴你的 GHCR Personal Access Token（输入时不可见）"
read -rsp "GHCR Token: " GHCR_TOKEN
 echo
 echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GITHUB_OWNER" --password-stdin

# 3. 创建项目目录
echo "[3/7] 创建项目目录 $PROJECT_DIR..."
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# 4. 拉取镜像
echo "[4/7] 拉取最新镜像 $IMAGE..."
docker pull "$IMAGE"

# 5. 创建 .env 文件（如不存在）
echo "[5/7] 检查 .env 配置..."
if [ ! -f ".env" ]; then
    cat > .env <<EOF
REDIS_URL=redis://redis:6379/0
POLL_INTERVAL_SECONDS=1.5
METAR_TTL_SECONDS=7200
USER_AGENT=MyMetarApp/1.0 (htsao2000@gmail.com)
HTTP_TIMEOUT=15.0
LOG_LEVEL=INFO
EOF
    echo "已创建 .env 模板，请编辑 $PROJECT_DIR/.env 填入真实邮箱/Token 后再启动。"
    echo "部署暂停。编辑完成后重新运行本脚本，或直接执行 docker compose up -d"
    exit 0
fi

# 6. 下载/更新 docker-compose.yml（如果仓库中已有，也可从 GitHub raw 下载）
echo "[6/7] 准备 docker-compose.yml..."
if [ ! -f "docker-compose.yml" ]; then
    echo "本地没有 docker-compose.yml，请从开发机上传到 $PROJECT_DIR"
    echo "部署暂停。上传完成后重新运行本脚本，或直接执行 docker compose up -d"
    exit 0
fi

# 7. 启动服务
echo "[7/7] 启动容器..."
docker compose up -d --remove-orphans

sleep 5
echo "=========================================="
echo "部署完成！检查状态："
echo "=========================================="
docker compose ps
docker compose logs --tail=20 web

echo ""
echo "健康检查：curl http://127.0.0.1:8000/health"
echo "测试接口：curl 'http://127.0.0.1:8000/api/v1/metar?icao=KJFK'"
