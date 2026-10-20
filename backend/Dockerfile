# 使用 Python 3.11 官方镜像作为基础镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 配置 Debian 镜像源（使用清华镜像）
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources || \
    sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list || true

# 安装系统依赖（添加重试机制）
RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    curl \
    git \
    pkg-config \
    default-libmysqlclient-dev \
    clamav \
    clamav-freshclam \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Upload inspection fails closed when signatures are missing. Keep a usable
# signature database in the image; refresh it as part of image maintenance.
RUN freshclam --stdout && \
    printf 'verification scanner build check\n' > /tmp/verification-scan-check && \
    clamscan --no-summary /tmp/verification-scan-check && \
    rm /tmp/verification-scan-check

# 复制依赖文件（注意：构建上下文是项目根目录）
COPY backend/requirements.txt .

# 安装 Python 依赖
RUN pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple && \
    pip config set global.trusted-host mirrors.cloud.tencent.com && \
    pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install gunicorn daphne

# 复制项目文件
COPY backend .

# 创建必要的目录
RUN mkdir -p /app/logs /app/static /app/media

# 设置权限
RUN chmod +x manage.py

# 暴露端口
EXPOSE 8001

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8001/health/ || exit 1

# 默认命令（可以在 docker-compose 中覆盖）
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8001"]

