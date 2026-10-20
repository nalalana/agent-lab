# 项目配置说明

## 📁 配置文件结构

所有配置文件统一管理在 `config/` 目录下：

```
config/
├── docker/              # Docker 配置
│   ├── compose.dev.yml      # 开发环境 Docker Compose
│   ├── compose.prod.yml     # 生产环境 Docker Compose
│   └── Dockerfile.backend   # 后端 Dockerfile
└── env/                 # 环境变量示例
    ├── .dev.example        # 开发环境变量示例
    ├── .test.example       # 测试环境变量示例
    └── .prod.example       # 生产环境变量示例

```

## 🚀 快速开始

### 开发测试环境

平时开发、联调、内部测试使用这套环境，不承接正式用户流量。

```bash
# 1. 配置环境变量
cp config/env/.dev.example config/env/.dev
# 编辑 config/env/.dev，填入配置

# 2. 启动服务
docker-compose -f config/docker/compose.dev.yml --env-file config/env/.dev up -d
```

### 线上环境

给正式用户使用这套环境。线上环境必须使用生产数据库、生产 Redis、生产密钥和正式域名。

```bash
# 1. 配置环境变量
cp config/env/.prod.example config/env/.prod
# 编辑 config/env/.prod，填入真实配置

# 2. 启动服务。默认连接托管 PostgreSQL/Redis，不启动本地 postgres/redis 容器。
docker-compose -f config/docker/compose.prod.yml --env-file config/env/.prod up -d

# 3. 初始化数据库
docker-compose -f config/docker/compose.prod.yml exec backend python manage.py migrate
```

如需临时回退到 Docker 内 PostgreSQL/Redis，可显式启用本地状态服务：

```bash
COMPOSE_PROFILES=local-state docker-compose -f config/docker/compose.prod.yml --env-file config/env/.prod up -d
```

## 📋 配置文件说明

### Docker 配置

- **开发测试环境**: `config/docker/compose.dev.yml` - 包含 PostgreSQL、Redis、Immudb、后端服务
- **线上环境**: `config/docker/compose.prod.yml` - 默认启动 Immudb、后端、Celery；Docker 内 PostgreSQL/Redis 仅作为 `local-state` profile 下的回退服务
- **Dockerfile**: `backend/Dockerfile` - 后端应用镜像构建

### 环境变量

所有环境变量文件都是示例文件（`.example`），不包含真实密码。使用时：

1. 复制示例文件：`cp config/env/.prod.example config/env/.prod`
2. 编辑配置文件，填入真实值
3. **重要**：确保真实配置文件（`.dev`、`.test`、`.prod`）不被提交到 Git

环境切换由 `DJANGO_SETTINGS_MODULE` 和 `DJANGO_ENVIRONMENT` 控制：

- 开发测试服务：`DJANGO_SETTINGS_MODULE=config.settings.dev`，`DJANGO_ENVIRONMENT=development`
- 线上服务：`DJANGO_SETTINGS_MODULE=config.settings.prod`，`DJANGO_ENVIRONMENT=production`
- 自动化测试：`DJANGO_SETTINGS_MODULE=config.settings.test`，`DJANGO_ENVIRONMENT=testing`

Docker Compose 中也支持从 env 文件覆盖 `DJANGO_SETTINGS_MODULE`。默认情况下，`compose.dev.yml` 使用开发测试配置，`compose.prod.yml` 使用线上配置。


### 后端配置

Django 配置文件位于 `backend/config/settings/`：
- `base.py` - 基础配置（所有环境共享）
- `dev.py` - 开发测试服务配置
- `test.py` - 自动化测试配置
- `prod.py` - 线上服务配置

### 图片内容安全配置

生产环境如启用 COS 图片审核，必须配置：

```bash
IMAGE_MODERATION_PROVIDER=cos
IMAGE_MODERATION_COS_DETECT_TYPE=Porn,Terrorism,Politics,Ads
IMAGE_MODERATION_COS_CALLBACK=https://your-domain.example/api/moderation/cos/callback
IMAGE_MODERATION_CALLBACK_SECRET=<random-high-entropy-secret>
IMAGE_MODERATION_ALLOW_UNTRACKED=false
```

说明：

- `IMAGE_MODERATION_CALLBACK_SECRET` 必须配置；回调请求需带 `X-Image-Moderation-Secret`。
- `IMAGE_MODERATION_ALLOW_UNTRACKED` 默认就是 `false`，只有开发或迁移窗口才可显式设为 `true`。
- 本地联调可用 `IMAGE_MODERATION_PROVIDER=local` 和 `IMAGE_MODERATION_LOCAL_DECISION=approved` 模拟审核通过。

## 🔍 常用操作

### 查看服务状态
```bash
docker-compose -f config/docker/compose.prod.yml ps
```

### 查看日志
```bash
docker-compose -f config/docker/compose.prod.yml logs -f backend
docker-compose -f config/docker/compose.prod.yml logs -f postgres
```

### 停止服务
```bash
docker-compose -f config/docker/compose.prod.yml down
```

### PostgreSQL 备份
```bash
docker-compose -f config/docker/compose.prod.yml exec postgres pg_dump -U $POSTGRES_USER $POSTGRES_DB > backup.sql
```

使用托管 PostgreSQL 后，生产备份应以腾讯云备份/快照策略为主；上面的 Docker 内 PostgreSQL 备份命令仅适用于启用了 `local-state` profile 的回退场景。

## ⚠️ 注意事项

### 0030 企业正式约束：开发库必须重建

`company.0030` 是平台上线前的 breaking schema hardening，只面向全新数据库。它不会清洗、合并或回填已有的测试脏数据，也不应直接应用到旧开发数据库。

应用该迁移前，请停止服务并确认目标确实是可丢弃的开发数据库，然后删除并重新创建该数据库，再从 `0001` 执行完整的 `python manage.py migrate`。迁移文件必须保留，不 squash、不修改历史迁移。迁移完成后再次运行 `python manage.py migrate`，结果应为 no-op。正式部署前还必须在空 PostgreSQL 数据库检查 0030 的 check/partial unique/expression unique 约束均已实际创建。

1. **敏感信息**：所有包含密码、密钥的配置文件都应添加到 `.gitignore`
2. **环境隔离**：不同环境的配置严格分离
3. **路径引用**：Docker Compose 文件中的路径是相对于配置文件所在目录
4. **PostgreSQL 日志**：日志文件位于 `./logs/postgresql/pg_log_YYYYMMDD_HHMMSS.log`

