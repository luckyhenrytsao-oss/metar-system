# 多阶段构建：先编译依赖，再复制代码，减少最终镜像体积
FROM python:3.11-slim AS builder

# 安装编译依赖所需的系统包
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境，隔离依赖
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 先复制 requirements 以利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 运行阶段：使用干净的基础镜像
FROM python:3.11-slim

# 非 root 用户运行，提高安全性
RUN groupadd -r metar && useradd -r -g metar metar

# 从 builder 阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 设置工作目录
WORKDIR /app

# 复制应用代码
COPY --chown=metar:metar app ./app

# 切换到非 root 用户
USER metar

# 暴露 FastAPI 默认端口
EXPOSE 8000

# 启动命令：使用 Uvicorn 运行 FastAPI
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
