# 部署备忘

## 已配置的固定信息

- **联系邮箱**：`htsao2000@gmail.com`（已写入 `app/config.py`、`.env.example`、`docker-compose.yml`、`scripts/vps-first-deploy.sh`）
- **GitHub 账户**：`https://github.com/luckyhenrytsao-oss`
- **仓库名**：`metar-system`（**尚未创建，需要手动新建**）
- **GHCR 镜像地址**：`ghcr.io/luckyhenrytsao-oss/metar-system:latest`

## 还缺的信息 / 需要你做的事

1. **在 GitHub 新建仓库**
   - 访问 https://github.com/new
   - Owner 选择 `luckyhenrytsao-oss`
   - Repository name 填 `metar-system`
   - 选择 Public 或 Private（Private 不影响 GHCR，但 CI/CD  secrets 都要重新核对）

2. **生成本地 Git 仓库并 push**
   ```bash
   cd D:/henry_project/M2
   git init
   git add .
   git commit -m "Initial commit: METAR high-speed collection and distribution system"
   git branch -M main
   git remote add origin https://github.com/luckyhenrytsao-oss/metar-system.git
   git push -u origin main
   ```

3. **创建 GHCR Token**
   - 访问 https://github.com/settings/tokens/new
   - 勾选 `write:packages`
   - 保存生成的 token（只显示一次）

4. **配置 GitHub Actions Secrets**
   在仓库页面 `Settings -> Secrets and variables -> Actions -> New repository secret` 添加：
   | Secret | 值 |
   |---|---|
   | `GHCR_TOKEN` | 上一步生成的 token |
   | `VPS_HOST` | `47.251.25.183` |
   | `VPS_PORT` | `2222` |
   | `VPS_USER` | `root` |
   | `VPS_SSH_KEY` | 你的 SSH 私钥全文（含 `-----BEGIN...` 和 `-----END...`） |

5. **VPS 首次手动部署**
   将 `docker-compose.yml`、`.env.example`、`scripts/vps-first-deploy.sh` 传到 VPS `/opt/metar-system/`，然后执行：
   ```bash
   bash /opt/metar-system/vps-first-deploy.sh
   ```
   按提示输入 GHCR Token。

6. **可选但推荐**
   - 到 https://synopticdata.com 申请独立 Token，填入 VPS 的 `/opt/metar-system/.env` 中的 `WEATHERGOV_TOKEN`
   - 配置 Nginx 反向代理 + HTTPS 证书（如果需要通过域名/443 访问）

## 验证命令

```bash
# VPS 上
curl http://127.0.0.1:8000/health
curl 'http://127.0.0.1:8000/api/v1/metar?icao=KJFK'

# 本地通过 SSH 隧道测试
ssh -p 2222 -L 8000:127.0.0.1:8000 root@47.251.25.183 -N
curl http://127.0.0.1:8000/health
```
