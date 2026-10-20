"""Base (shared) Django settings for all environments.

Split from `touming.settings` to enable env-specific overrides.
"""

from pathlib import Path
import os
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from config.enterprise_verification_secrets import load_enterprise_verification_secrets
from config.agsx_runtime_env import load_agsx_runtime_env

# 先定义路径，以便后续加载 .env 文件
BASE_DIR = Path(__file__).resolve().parents[2]
# 项目根目录（backend 的父目录）
PROJECT_ROOT = BASE_DIR.parent

# 根据 DJANGO_SETTINGS_MODULE 确定环境，加载对应的 .env 文件
# 例如：config.settings.dev -> .dev, config.settings.prod -> .prod
settings_module = os.environ.get('DJANGO_SETTINGS_MODULE', '')
env_file_name = None
if 'dev' in settings_module:
    env_file_name = '.dev'
elif 'prod' in settings_module:
    env_file_name = '.prod'
elif 'test' in settings_module:
    env_file_name = '.test'

# 加载环境变量文件
# 优先级：1. Docker 容器内挂载的文件 2. 项目目录下的文件 3. 项目根目录的 .env
env_loaded = False
env_files_to_try = []


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return default or []
    return [item.strip() for item in value.split(',') if item.strip()]


def _env_int_list(name: str) -> list[int]:
    values = _env_list(name)
    try:
        return [int(value) for value in values]
    except ValueError as exc:
        raise ImproperlyConfigured(
            f'{name} must be a comma-separated integer list'
        ) from exc


def _environment_from_settings_module(module_name: str) -> str:
    if 'prod' in module_name:
        return 'production'
    if 'test' in module_name:
        return 'testing'
    return 'development'

if env_file_name:
    # Docker 容器内挂载的文件路径（如果存在）
    docker_env_file = BASE_DIR / f'.env{env_file_name}'
    if docker_env_file.exists():
        env_files_to_try.append(('Docker挂载', docker_env_file))
    
    # 项目目录下的环境变量文件
    project_env_file = PROJECT_ROOT / 'config' / 'env' / env_file_name
    if project_env_file.exists():
        env_files_to_try.append(('项目配置', project_env_file))
    
    # 项目根目录的 .env 文件
    root_env_file = PROJECT_ROOT / '.env'
    if root_env_file.exists():
        env_files_to_try.append(('根目录', root_env_file))
else:
    # 如果没有明确的环境设置，尝试加载项目根目录的 .env 文件
    root_env_file = PROJECT_ROOT / '.env'
    if root_env_file.exists():
        env_files_to_try.append(('根目录', root_env_file))
    # 也尝试加载默认的开发环境文件
    dev_env_file = PROJECT_ROOT / 'config' / 'env' / '.dev'
    if dev_env_file.exists():
        env_files_to_try.append(('默认开发', dev_env_file))

# 按优先级加载环境变量文件
# .env 文件的值优先级更高，会覆盖已存在的环境变量（包括 Docker Compose 设置的）
for desc, env_file in env_files_to_try:
    try:
        if env_file.stat().st_size == 0:
            print(f"[settings] 跳过空环境变量文件 ({desc}): {env_file}")
            continue
        # override=True 确保 .env 文件中的值能够覆盖已存在的环境变量
        # 这样 .dev 文件中的配置（如 POSTGRES_HOST=postgres）会优先于 Docker Compose 的设置
        if not load_dotenv(str(env_file), override=True):
            print(f"[settings] 跳过未加载任何变量的环境变量文件 ({desc}): {env_file}")
            continue
        env_loaded = True
        print(f"[settings] 已加载环境变量文件 ({desc}): {env_file} (覆盖已存在的环境变量)")
        break  # 找到第一个存在的文件就停止
    except Exception as e:
        print(f"[settings] 警告: 加载环境变量文件失败 ({desc}): {env_file}, 错误: {e}")

if not env_loaded:
    print(f"[settings] 警告: 未找到任何环境变量文件，DJANGO_SETTINGS_MODULE={settings_module}")
    print(f"[settings] 尝试的路径: {[str(f[1]) for f in env_files_to_try]}")

# Phase4-C keeps every operator-provided AGSX value outside config/env. The
# deployment script renders a runtime-only allowlisted file after the cloud
# Tool is verified, and production Compose mounts it read-only. Load it last so
# stale AGSX placeholders in a legacy .prod file cannot override production.
agsx_runtime_env_file = os.getenv('AGSX_RUNTIME_ENV_FILE', '').strip()
if agsx_runtime_env_file:
    loaded_agsx_runtime_env = load_agsx_runtime_env(agsx_runtime_env_file)
    print(f"[settings] 已加载 AGSX 运行配置: {loaded_agsx_runtime_env}")

# Keep compatibility with existing properties
# 注意：环境变量优先于 properties.py 中的值
try:
    from properties import PASSWORD, POSTGRES_USER as _PROPS_POSTGRES_USER, POSTGRES_PASSWORD as _PROPS_POSTGRES_PASSWORD, POSTGRES_HOST as _PROPS_POSTGRES_HOST, POSTGRES_PORT as _PROPS_POSTGRES_PORT, POSTGRES_DB as _PROPS_POSTGRES_DB, ENVIRONMENT  # noqa: F401,F403
except ImportError:
    # 如果 properties.py 不存在，使用默认值
    PASSWORD = os.getenv('IMMUDB_ADMIN_PASSWORD', '')
    _PROPS_POSTGRES_USER = 'postgres'
    _PROPS_POSTGRES_PASSWORD = 'postgres'
    _PROPS_POSTGRES_HOST = 'localhost'
    _PROPS_POSTGRES_PORT = '5432'
    _PROPS_POSTGRES_DB = 'touming_dev'
    ENVIRONMENT = os.getenv('DJANGO_ENVIRONMENT', 'development')

ENVIRONMENT = os.getenv(
    'DJANGO_ENVIRONMENT',
    _environment_from_settings_module(settings_module),
)
os.environ['DJANGO_ENVIRONMENT'] = ENVIRONMENT

# 环境变量优先，如果环境变量不存在则使用 properties.py 的值
# 先检查环境变量是否存在（用于调试）
postgres_host_env = os.getenv('POSTGRES_HOST')
postgres_user_env = os.getenv('POSTGRES_USER')
postgres_db_env = os.getenv('POSTGRES_DB')

POSTGRES_USER = os.getenv('POSTGRES_USER', _PROPS_POSTGRES_USER)
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD', _PROPS_POSTGRES_PASSWORD)
POSTGRES_HOST = os.getenv('POSTGRES_HOST', _PROPS_POSTGRES_HOST)
POSTGRES_PORT = os.getenv('POSTGRES_PORT', _PROPS_POSTGRES_PORT)
POSTGRES_DB = os.getenv('POSTGRES_DB', _PROPS_POSTGRES_DB)

# Admin安全配置
ADMIN_ALLOWED_IPS = os.getenv('ADMIN_ALLOWED_IPS', '').split(',') if os.getenv('ADMIN_ALLOWED_IPS') else []
TRUSTED_PROXY_IPS = _env_list('TRUSTED_PROXY_IPS', [])

MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
MEDIA_URL = '/media/'

SECRET_KEY = os.getenv('SECRET_KEY') or os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if 'prod' in settings_module:
        raise ImproperlyConfigured('SECRET_KEY or DJANGO_SECRET_KEY is required in production')
    SECRET_KEY = "django-insecure-dev-only-change-me"
SMS_RATE_LIMIT_HASH_SALT = os.getenv('SMS_RATE_LIMIT_HASH_SALT', SECRET_KEY)

DEBUG = False

ALLOWED_HOSTS = _env_list('ALLOWED_HOSTS', ['localhost', '127.0.0.1'])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    'django_crontab',
    'captcha',
    'corsheaders',
    'channels',
    "apps.user",
    "apps.news",
    "apps.follow",
    "apps.opengate",
    "apps.company",
    "apps.ops",
    "apps.iam",
    "apps.notification",
    "apps.sso",
    "apps.runtime",
    "apps.agent_runtime",
    "apps.risk_control",
    "apps.moderation",
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'common.middleware.DevelopmentDatabaseRecoveryMiddleware',
    # 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.runtime.legacy_control.LegacyRuntimeReadOnlyMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'common.middleware.AdminIPWhitelistMiddleware',  # Admin IP白名单
]

ROOT_URLCONF = "config.urls"

CORS_ALLOW_CREDENTIALS = _env_bool('CORS_ALLOW_CREDENTIALS', True)
CORS_ORIGIN_ALLOW_ALL = _env_bool('CORS_ORIGIN_ALLOW_ALL', False)

# 允许的请求头名称（供预检 OPTIONS 返回 Access-Control-Allow-Headers），不能填 origin 地址
CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "access-token",
    "content-type",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
)

# 当 CORS_ORIGIN_ALLOW_ALL=False 时生效；当前为 True 时会按请求的 Origin 回填
CORS_ALLOWED_ORIGINS = _env_list('CORS_ALLOWED_ORIGINS', [])

CORS_ALLOW_METHODS = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / 'templates'],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': POSTGRES_DB,
        'USER': POSTGRES_USER,
        'PASSWORD': POSTGRES_PASSWORD,
        'HOST': POSTGRES_HOST,
        'PORT': POSTGRES_PORT,
    }
}

# 确保日志目录存在
LOGS_DIR = PROJECT_ROOT / 'logs' / 'django'
LOGS_DIR.mkdir(parents=True, exist_ok=True)

print(LOGS_DIR)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
        "sandbox_metric_json": {
            "()": "config.logging.SandboxMetricJsonFormatter",
        },
    },
    "handlers": {
        "file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(PROJECT_ROOT / 'logs' / 'django' / 'touming_django.log'),
            "maxBytes": 10485760,
            "backupCount": 50,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "sandbox_metrics_file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(PROJECT_ROOT / 'logs' / 'django' / 'sandbox_metrics.jsonl'),
            "maxBytes": 10485760,
            "backupCount": 20,
            "formatter": "sandbox_metric_json",
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": False,
        },
        "touming": {
            "handlers": ["file", "console"],
            "level": "DEBUG",
            "propagate": False,
        },
        # Tencent's SDK debug logger formats complete API responses; FaceID
        # responses contain identity fields, so debug propagation is forbidden.
        "tencentcloud_sdk_common": {
            "handlers": [],
            "level": "INFO",
            "propagate": False,
        },
        "agent_runtime.sandbox.metrics": {
            "handlers": ["sandbox_metrics_file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# Phase 4-B keeps the old tables queryable while centrally rejecting writes
# after P4-A has completed the 100% unified rollout observation gate.
OPENCLAW_LEGACY_WRITE_MODE = os.getenv(
    'OPENCLAW_LEGACY_WRITE_MODE',
    'read_only',
).strip().lower()
DATABASE_ROUTERS = ['apps.runtime.legacy_control.LegacyRuntimeWriteRouter']

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = 'zh-Hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = False

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'static'),
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

WEB3_PROVIDER_URI = 'https://mainnet.infura.io/v3/YOUR_INFURA_PROJECT_ID'

# Phase 1 unified model runtime. Provider keys stay in company/platform secret
# stores; only references to those credentials are allowed to enter Run context.
AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS = int(os.getenv('AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS', '30'))
AGENT_RUNTIME_MODEL_MAX_RETRIES = int(os.getenv('AGENT_RUNTIME_MODEL_MAX_RETRIES', '2'))
AGENT_RUNTIME_MODEL_RETRY_BASE_DELAY_SECONDS = float(
    os.getenv('AGENT_RUNTIME_MODEL_RETRY_BASE_DELAY_SECONDS', '0.2')
)
AGENT_RUNTIME_SSE_QUEUE_SIZE = int(os.getenv('AGENT_RUNTIME_SSE_QUEUE_SIZE', '64'))
AGENT_RUNTIME_ALLOW_INSECURE_MODEL_BASE_URL = _env_bool(
    'AGENT_RUNTIME_ALLOW_INSECURE_MODEL_BASE_URL',
    False,
)
AGENT_RUNTIME_MODEL_ALLOWED_HOSTS = tuple(
    host.strip().lower()
    for host in os.getenv('AGENT_RUNTIME_MODEL_ALLOWED_HOSTS', '').split(',')
    if host.strip()
)
# Platform-managed model. The API key must only be supplied by deployment secrets.
AGENT_RUNTIME_PLATFORM_PROVIDER_ID = os.getenv('AGENT_RUNTIME_PLATFORM_PROVIDER_ID', 'custom-qwen').strip()
AGENT_RUNTIME_PLATFORM_MODEL_ID = os.getenv('AGENT_RUNTIME_PLATFORM_MODEL_ID', 'qwen3.8-27b').strip()
AGENT_RUNTIME_PLATFORM_BASE_URL = os.getenv(
    'AGENT_RUNTIME_PLATFORM_BASE_URL',
    'http://112.95.75.67:9018/v1',
).strip()
AGENT_RUNTIME_PLATFORM_API_KEY = os.getenv('AGENT_RUNTIME_PLATFORM_API_KEY', '').strip()
AGENT_RUNTIME_PLATFORM_DISABLE_THINKING = _env_bool(
    'AGENT_RUNTIME_PLATFORM_DISABLE_THINKING',
    True,
)
# Memory Gateway uses these values to reach the trusted OpenViking data plane.
OPENVIKING_BASE_URL = os.getenv('OPENVIKING_BASE_URL', '').strip()
OPENVIKING_API_KEY = os.getenv('OPENVIKING_API_KEY', '').strip()
OPENVIKING_TIMEOUT = int(os.getenv('OPENVIKING_TIMEOUT', '30'))
OPENVIKING_CLUSTER_NAME = os.getenv('OPENVIKING_CLUSTER_NAME', 'TSDS-OpenViking').strip()
OPENVIKING_CLUSTER_CAPACITY = int(os.getenv('OPENVIKING_CLUSTER_CAPACITY', '1000'))
OPENVIKING_CREDENTIALS_DIR = os.getenv(
    'OPENVIKING_CREDENTIALS_DIR', '/etc/touming/openviking'
).strip()
OPENVIKING_ROOT_CREDENTIAL_REF = os.getenv(
    'OPENVIKING_ROOT_CREDENTIAL_REF', 'file:root.key'
).strip()
OPENVIKING_ISOLATION_CREDENTIAL_REF = os.getenv(
    'OPENVIKING_ISOLATION_CREDENTIAL_REF', 'file:isolation-sentinel.key'
).strip()
OPENVIKING_TENANT_RATE_LIMIT_PER_MINUTE = int(
    os.getenv('OPENVIKING_TENANT_RATE_LIMIT_PER_MINUTE', '240')
)
OPENVIKING_TENANT_MAX_CONCURRENT = int(
    os.getenv('OPENVIKING_TENANT_MAX_CONCURRENT', '8')
)
OPENVIKING_MEMORY_POLL_SECONDS = int(
    os.getenv('OPENVIKING_MEMORY_POLL_SECONDS', '5')
)
OPENVIKING_MEMORY_MAX_POLLS = int(
    os.getenv('OPENVIKING_MEMORY_MAX_POLLS', '120')
)

# Phase 2 AGSX code sandbox. Management credentials remain in Backend/Celery
# and are never copied into the sandbox process environment.
AGSX_ENABLED = _env_bool('AGSX_ENABLED', False)
E2B_DOMAIN = os.getenv('E2B_DOMAIN', '').strip()
E2B_API_KEY = os.getenv('E2B_API_KEY', '').strip()
AGSX_TEMPLATE = os.getenv('AGSX_TEMPLATE', '').strip()
AGSX_DEFAULT_CPU_CORES = int(os.getenv('AGSX_DEFAULT_CPU_CORES', '2'))
AGSX_DEFAULT_MEMORY_MIB = int(os.getenv('AGSX_DEFAULT_MEMORY_MIB', '2048'))
AGSX_DEFAULT_TTL_SECONDS = int(os.getenv('AGSX_DEFAULT_TTL_SECONDS', '300'))
AGSX_NETWORK_POLICY = os.getenv('AGSX_NETWORK_POLICY', 'isolated').strip().lower()
AGSX_REQUEST_TIMEOUT_SECONDS = int(os.getenv('AGSX_REQUEST_TIMEOUT_SECONDS', '60'))
AGSX_SANDBOX_CLEANUP_GRACE_SECONDS = int(
    os.getenv('AGSX_SANDBOX_CLEANUP_GRACE_SECONDS', '120')
)
AGSX_REAPER_BATCH_SIZE = int(os.getenv('AGSX_REAPER_BATCH_SIZE', '100'))
AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES = int(
    os.getenv('AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES', '25')
)
AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR = _env_bool(
    'AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR',
    True,
)
# AGSX pricing is deployment-specific. Values use integer micro-units to avoid
# floating-point drift; one CNY is 1,000,000 micro-units when currency is CNY.
AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G = int(
    os.getenv('AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G', '0')
)
AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS = int(
    os.getenv('AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS', '0')
)
AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS = int(
    os.getenv('AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS', '0')
)
AGSX_COST_CURRENCY = os.getenv('AGSX_COST_CURRENCY', 'CNY').strip().upper()
# Temporary rollout switch: effective resources are observed and reported even
# when strict enforcement is disabled. Enable after the 2C2G Tool is corrected.
AGSX_ENFORCE_EFFECTIVE_RESOURCES = _env_bool('AGSX_ENFORCE_EFFECTIVE_RESOURCES', False)

# Phase 3 operational alerts. Values are intentionally explicit so deployment
# can tune alert sensitivity without changing application code.
AGENT_RUNTIME_ALERTS_ENABLED = _env_bool('AGENT_RUNTIME_ALERTS_ENABLED', True)
AGENT_RUNTIME_ALERT_ORPHAN_MINUTES = int(
    os.getenv('AGENT_RUNTIME_ALERT_ORPHAN_MINUTES', '15')
)
AGENT_RUNTIME_ALERT_FAILURE_WINDOW_MINUTES = int(
    os.getenv('AGENT_RUNTIME_ALERT_FAILURE_WINDOW_MINUTES', '15')
)
AGENT_RUNTIME_ALERT_FAILURE_RATE = float(
    os.getenv('AGENT_RUNTIME_ALERT_FAILURE_RATE', '0.2')
)
AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS = int(
    os.getenv('AGENT_RUNTIME_ALERT_FAILURE_MIN_RUNS', '10')
)
AGENT_RUNTIME_ALERT_QUOTA_USAGE_RATIO = float(
    os.getenv('AGENT_RUNTIME_ALERT_QUOTA_USAGE_RATIO', '0.8')
)
AGENT_RUNTIME_ALERT_DEDUP_SECONDS = int(
    os.getenv('AGENT_RUNTIME_ALERT_DEDUP_SECONDS', '900')
)
AGENT_RUNTIME_ALERT_REDIS_PREFIX = os.getenv(
    'AGENT_RUNTIME_ALERT_REDIS_PREFIX',
    'touming:agent-runtime:alerts',
).strip()
AGSX_ALERT_FAILURE_WINDOW_MINUTES = int(
    os.getenv('AGSX_ALERT_FAILURE_WINDOW_MINUTES', '15')
)
AGSX_ALERT_FAILURE_RATE = float(os.getenv('AGSX_ALERT_FAILURE_RATE', '0.2'))
AGSX_ALERT_FAILURE_MIN_CREATES = int(
    os.getenv('AGSX_ALERT_FAILURE_MIN_CREATES', '5')
)

AGENT_RUNTIME_SANDBOX_REDIS_PREFIX = os.getenv(
    'AGENT_RUNTIME_SANDBOX_REDIS_PREFIX',
    'touming:agent-runtime:sandbox-leases',
).strip()
AGENT_RUNTIME_SANDBOX_STDOUT_LIMIT_BYTES = int(
    os.getenv('AGENT_RUNTIME_SANDBOX_STDOUT_LIMIT_BYTES', '65536')
)
AGENT_RUNTIME_SANDBOX_STDERR_LIMIT_BYTES = int(
    os.getenv('AGENT_RUNTIME_SANDBOX_STDERR_LIMIT_BYTES', '65536')
)
AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_BYTES = int(
    os.getenv('AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_BYTES', str(50 * 1024 * 1024))
)
AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES = int(
    os.getenv('AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES', str(200 * 1024 * 1024))
)
AGENT_RUNTIME_SANDBOX_MAX_OVERFLOW_BYTES = int(
    os.getenv('AGENT_RUNTIME_SANDBOX_MAX_OVERFLOW_BYTES', str(5 * 1024 * 1024))
)
AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT = int(
    os.getenv('AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT', '50')
)
AGENT_RUNTIME_WORKSPACE_READ_MAX_BYTES = int(
    os.getenv('AGENT_RUNTIME_WORKSPACE_READ_MAX_BYTES', str(1024 * 1024))
)
AGENT_RUNTIME_WORKSPACE_PDF_MAX_BYTES = int(
    os.getenv('AGENT_RUNTIME_WORKSPACE_PDF_MAX_BYTES', str(10 * 1024 * 1024))
)
AGENT_RUNTIME_WORKSPACE_PDF_MAX_PAGES = int(
    os.getenv('AGENT_RUNTIME_WORKSPACE_PDF_MAX_PAGES', '100')
)
AGENT_RUNTIME_WORKSPACE_PDF_MAX_TEXT_CHARS = int(
    os.getenv('AGENT_RUNTIME_WORKSPACE_PDF_MAX_TEXT_CHARS', '120000')
)
AGENT_RUNTIME_ARTIFACT_COS_BUCKET = os.getenv('AGENT_RUNTIME_ARTIFACT_COS_BUCKET', '').strip()
AGENT_RUNTIME_ARTIFACT_COS_REGION = os.getenv('AGENT_RUNTIME_ARTIFACT_COS_REGION', '').strip()
AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID = os.getenv(
    'AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID', ''
).strip()
AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY = os.getenv(
    'AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY', ''
).strip()
AGENT_RUNTIME_ARTIFACT_COS_TOKEN = os.getenv('AGENT_RUNTIME_ARTIFACT_COS_TOKEN', '').strip()
AGENT_RUNTIME_ARTIFACT_COS_PREFIX = os.getenv(
    'AGENT_RUNTIME_ARTIFACT_COS_PREFIX', 'agent-runtime/v1'
).strip('/')
RUNTIME_SNAPSHOT_RETENTION_DAYS = int(os.getenv('RUNTIME_SNAPSHOT_RETENTION_DAYS', '30'))
TENCENT_CLOUD_SECRET_ID = os.getenv('TENCENT_CLOUD_SECRET_ID', '').strip()
TENCENT_CLOUD_SECRET_KEY = os.getenv('TENCENT_CLOUD_SECRET_KEY', '').strip()
# IMMU数据库配置 - 根据环境动态选择
if ENVIRONMENT == 'production':
    IMMU_URL = '192.144.140.35:3322'
else:
    IMMU_URL = 'localhost:3322'

IMMU_DEFAULT_EXPIRE_TIME = None
IMMU_DEFAULT_DB = 'defaultdb'
IMMU_USER = 'immudb'
IMMU_PASSWORD = PASSWORD
IMMU_PUBLIC_KEY = None

DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760

# Tencent Cloud account credentials are shared by services unless a product
# specific credential pair is configured below.
TENCENTCLOUD_SECRET_ID = os.getenv("TENCENTCLOUD_SECRET_ID", "").strip()
TENCENTCLOUD_SECRET_KEY = os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip()

# Tencent ESS third-party application integration. Secrets intentionally have
# no source-code defaults and are never inherited from unrelated products.
TENCENT_ESS_APP_ID = os.getenv('TENCENT_ESS_APP_ID', '').strip()
TENCENT_ESS_SECRET_ID = os.getenv('TENCENT_ESS_SECRET_ID', '').strip()
TENCENT_ESS_SECRET_KEY = os.getenv('TENCENT_ESS_SECRET_KEY', '').strip()
TENCENT_ESS_ENDPOINT = os.getenv(
    'TENCENT_ESS_ENDPOINT',
    'essbasic.tencentcloudapi.com',
).strip()
TENCENT_ESS_REGION = os.getenv('TENCENT_ESS_REGION', 'ap-guangzhou').strip()
TENCENT_ESS_CALLBACK_ENCRYPT_KEY = os.getenv(
    'TENCENT_ESS_CALLBACK_ENCRYPT_KEY',
    '',
).strip()
TENCENT_ESS_CALLBACK_VERIFY_TOKEN = os.getenv(
    'TENCENT_ESS_CALLBACK_VERIFY_TOKEN',
    '',
).strip()
TENCENT_ESS_AUTHORIZATION_TYPES = _env_int_list('TENCENT_ESS_AUTHORIZATION_TYPES')
TENCENT_ESS_CONSOLE_ENDPOINT = os.getenv('TENCENT_ESS_CONSOLE_ENDPOINT', 'H5').strip()
TENCENT_ESS_INITIATING_TIMEOUT_SECONDS = int(
    os.getenv('TENCENT_ESS_INITIATING_TIMEOUT_SECONDS', '900')
)
TENCENT_ESS_PENDING_RECONCILE_INTERVAL_SECONDS = int(
    os.getenv('TENCENT_ESS_PENDING_RECONCILE_INTERVAL_SECONDS', '900')
)
TENCENT_ESS_REFRESH_RATE_LIMIT_SECONDS = int(
    os.getenv('TENCENT_ESS_REFRESH_RATE_LIMIT_SECONDS', '10')
)

# Provider-neutral enterprise verification is fail-closed by default. Runtime
# credentials and local PII keys are read from a non-repository, read-only file.
COMPANY_VERIFICATION_ENABLED = _env_bool('COMPANY_VERIFICATION_ENABLED', False)
COMPANY_VERIFICATION_PROVIDER = os.getenv(
    'COMPANY_VERIFICATION_PROVIDER',
    'tencent_manual_v1',
).strip().lower()
COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT = os.getenv(
    'COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT',
    'production',
).strip().lower()
COMPANY_VERIFICATION_POLICY_VERSION = os.getenv(
    'COMPANY_VERIFICATION_POLICY_VERSION',
    'manual_v1',
).strip()
COMPANY_VERIFICATION_REGISTRY_MODE = os.getenv(
    'COMPANY_VERIFICATION_REGISTRY_MODE',
    'two_elements',
).strip().lower()
COMPANY_VERIFICATION_APPLICATION_TTL_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_APPLICATION_TTL_SECONDS', str(7 * 24 * 60 * 60))
)
COMPANY_VERIFICATION_REGISTRY_MAX_AGE_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_REGISTRY_MAX_AGE_SECONDS', str(24 * 60 * 60))
)
COMPANY_VERIFICATION_FACE_RESULT_MAX_AGE_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_FACE_RESULT_MAX_AGE_SECONDS', str(24 * 60 * 60))
)
COMPANY_VERIFICATION_FACE_QUERY_TTL_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_FACE_QUERY_TTL_SECONDS', str(3 * 24 * 60 * 60))
)
COMPANY_VERIFICATION_OPERATION_LEASE_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_OPERATION_LEASE_SECONDS', '60')
)
COMPANY_VERIFICATION_OPERATION_RETRY_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_OPERATION_RETRY_SECONDS', '30')
)
COMPANY_VERIFICATION_OPERATION_MAX_ATTEMPTS = int(
    os.getenv('COMPANY_VERIFICATION_OPERATION_MAX_ATTEMPTS', '3')
)
COMPANY_VERIFICATION_FACE_MAX_ATTEMPTS = int(
    os.getenv('COMPANY_VERIFICATION_FACE_MAX_ATTEMPTS', '3')
)
COMPANY_VERIFICATION_EVIDENCE_REFRESH_RATE_LIMIT_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_EVIDENCE_REFRESH_RATE_LIMIT_SECONDS', '10')
)
COMPANY_VERIFICATION_SECRETS_FILE = os.getenv(
    'COMPANY_VERIFICATION_SECRETS_FILE',
    '/etc/touming/enterprise-verification.json',
).strip()
_enterprise_verification_secrets = (
    load_enterprise_verification_secrets(COMPANY_VERIFICATION_SECRETS_FILE)
    if COMPANY_VERIFICATION_ENABLED
    else None
)
COMPANY_VERIFICATION_SECRETS_LOAD_ERROR = (
    _enterprise_verification_secrets.error if _enterprise_verification_secrets else ''
)
_enterprise_verification_secret_values = (
    _enterprise_verification_secrets.values if _enterprise_verification_secrets else {}
)
TENCENT_VERIFICATION_SECRET_ID = _enterprise_verification_secret_values.get(
    'tencent_secret_id', ''
)
TENCENT_VERIFICATION_SECRET_KEY = _enterprise_verification_secret_values.get(
    'tencent_secret_key', ''
)
TENCENT_VERIFICATION_TIMEOUT_SECONDS = int(
    os.getenv('TENCENT_VERIFICATION_TIMEOUT_SECONDS', '10')
)
TENCENT_OCR_ENDPOINT = os.getenv(
    'TENCENT_OCR_ENDPOINT', 'ocr.tencentcloudapi.com'
).strip()
TENCENT_OCR_REGION = os.getenv('TENCENT_OCR_REGION', 'ap-guangzhou').strip()
TENCENT_OCR_MAX_BASE64_BYTES = int(
    os.getenv('TENCENT_OCR_MAX_BASE64_BYTES', str(10 * 1024 * 1024))
)
TENCENT_FACEID_ENDPOINT = os.getenv(
    'TENCENT_FACEID_ENDPOINT', 'faceid.tencentcloudapi.com'
).strip()
TENCENT_FACEID_REGION = os.getenv('TENCENT_FACEID_REGION', 'ap-guangzhou').strip()
TENCENT_FACEID_RULE_ID = _enterprise_verification_secret_values.get(
    'faceid_rule_id', ''
)
TENCENT_FACEID_REDIRECT_URL = _enterprise_verification_secret_values.get(
    'faceid_redirect_url', ''
)
TENCENT_FACEID_TERMINAL_TYPE = os.getenv('TENCENT_FACEID_TERMINAL_TYPE', '0').strip()
COMPANY_VERIFICATION_MAX_FILE_BYTES = int(
    os.getenv('COMPANY_VERIFICATION_MAX_FILE_BYTES', str(5 * 1024 * 1024))
)
COMPANY_VERIFICATION_AUTHORIZATION_TEMPLATE_VERSION = os.getenv(
    'COMPANY_VERIFICATION_AUTHORIZATION_TEMPLATE_VERSION',
    'authorization-v1-draft',
).strip()
COMPANY_VERIFICATION_DOCUMENT_MAX_PIXELS = int(
    os.getenv('COMPANY_VERIFICATION_DOCUMENT_MAX_PIXELS', '40000000')
)
COMPANY_VERIFICATION_DOCUMENT_SCAN_TIMEOUT_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_DOCUMENT_SCAN_TIMEOUT_SECONDS', '30')
)
COMPANY_VERIFICATION_DOCUMENT_CHECK_MAX_AGE_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_DOCUMENT_CHECK_MAX_AGE_SECONDS', '86400')
)
COMPANY_VERIFICATION_ALLOW_MOCK = _env_bool(
    'COMPANY_VERIFICATION_ALLOW_MOCK',
    False,
)
COMPANY_VERIFICATION_REVIEW_ALLOW_MOCK = _env_bool(
    'COMPANY_VERIFICATION_REVIEW_ALLOW_MOCK',
    False,
)
COMPANY_VERIFICATION_NOTIFICATION_SENDER = os.getenv(
    'COMPANY_VERIFICATION_NOTIFICATION_SENDER',
    'apps.company.verification_reviews.send_in_app_verification_notification',
).strip()
COMPANY_VERIFICATION_JOB_MAX_ATTEMPTS = int(
    os.getenv('COMPANY_VERIFICATION_JOB_MAX_ATTEMPTS', '8')
)
COMPANY_VERIFICATION_NOTIFICATION_MAX_ATTEMPTS = int(
    os.getenv('COMPANY_VERIFICATION_NOTIFICATION_MAX_ATTEMPTS', '8')
)
COMPANY_VERIFICATION_RETENTION_DRY_RUN_AGE_SECONDS = int(
    os.getenv('COMPANY_VERIFICATION_RETENTION_DRY_RUN_AGE_SECONDS', str(180 * 86400))
)
COMPANY_VERIFICATION_RATE_LIMITS = {
    'start': (int(os.getenv('COMPANY_VERIFICATION_RATE_START_LIMIT', '5')), 60),
    'query': (int(os.getenv('COMPANY_VERIFICATION_RATE_QUERY_LIMIT', '60')), 60),
    'upload': (int(os.getenv('COMPANY_VERIFICATION_RATE_UPLOAD_LIMIT', '10')), 60),
    'submit': (int(os.getenv('COMPANY_VERIFICATION_RATE_SUBMIT_LIMIT', '10')), 60),
    'review': (int(os.getenv('COMPANY_VERIFICATION_RATE_REVIEW_LIMIT', '60')), 60),
    'suspend': (int(os.getenv('COMPANY_VERIFICATION_RATE_SUSPEND_LIMIT', '5')), 60),
}
COMPANY_VERIFICATION_PII_ENCRYPTION_KEY = _enterprise_verification_secret_values.get(
    'pii_encryption_key_b64', ''
)
COMPANY_VERIFICATION_PII_HMAC_KEY = _enterprise_verification_secret_values.get(
    'pii_hmac_key_b64', ''
)
COMPANY_VERIFICATION_PII_KEY_VERSION = _enterprise_verification_secret_values.get(
    'pii_key_version', ''
)
VERIFICATION_PRIVATE_COS_BUCKET = os.getenv(
    'VERIFICATION_PRIVATE_COS_BUCKET',
    '',
).strip()
VERIFICATION_PRIVATE_COS_REGION = os.getenv(
    'VERIFICATION_PRIVATE_COS_REGION',
    '',
).strip()
# COS and the Tencent verification APIs intentionally share the single
# read-only runtime credential file. The bucket/region remain non-secret
# environment settings, while SecretId/SecretKey are loaded only once from
# COMPANY_VERIFICATION_SECRETS_FILE.
VERIFICATION_PRIVATE_COS_SECRET_ID = TENCENT_VERIFICATION_SECRET_ID
VERIFICATION_PRIVATE_COS_SECRET_KEY = TENCENT_VERIFICATION_SECRET_KEY
VERIFICATION_PRIVATE_COS_TOKEN = ''
VERIFICATION_PRIVATE_COS_SECRETS_LOAD_ERROR = COMPANY_VERIFICATION_SECRETS_LOAD_ERROR
VERIFICATION_RETENTION_POLICY = os.getenv(
    'VERIFICATION_RETENTION_POLICY',
    '',
).strip()
COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY = os.getenv(
    'COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY',
    '',
).strip()
COMPANY_VERIFICATION_TEST_HMAC_KEY = os.getenv(
    'COMPANY_VERIFICATION_TEST_HMAC_KEY',
    '',
).strip()
COMPANY_VERIFICATION_TEST_KEY_VERSION = os.getenv(
    'COMPANY_VERIFICATION_TEST_KEY_VERSION',
    'test-v1',
).strip()
COMPANY_VERIFICATION_ESS_START_ENABLED = _env_bool(
    'COMPANY_VERIFICATION_ESS_START_ENABLED',
    False,
)
COMPANY_VERIFICATION_ESS_RECONCILE_ENABLED = _env_bool(
    'COMPANY_VERIFICATION_ESS_RECONCILE_ENABLED',
    False,
)

# Tencent Cloud captcha. App id / app secret are captcha-product credentials and
# must not be committed as defaults.
TENCENT_CAPTCHA_APP_ID = os.getenv("TENCENT_CAPTCHA_APP_ID", "").strip()
TENCENT_CAPTCHA_SECRET_KEY = os.getenv("TENCENT_CAPTCHA_SECRET_KEY", "").strip()
TENCENT_CAPTCHA_ENDPOINT = os.getenv("TENCENT_CAPTCHA_ENDPOINT", "captcha.tencentcloudapi.com").strip()
TENCENT_CAPTCHA_REGION = os.getenv("TENCENT_CAPTCHA_REGION", "ap-guangzhou").strip()
TENCENT_CAPTCHA_TOKEN_TTL = int(os.getenv("TENCENT_CAPTCHA_TOKEN_TTL", "300"))

# SMS verification code provider. Local development defaults to mock; production
# can set SMS_PROVIDER=tencent and provide the Tencent Cloud SMS credentials.
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "mock").strip().lower()
TENCENT_SMS = {
    "SECRET_ID": os.getenv("TENCENT_SMS_SECRET_ID", TENCENTCLOUD_SECRET_ID).strip(),
    "SECRET_KEY": os.getenv("TENCENT_SMS_SECRET_KEY", TENCENTCLOUD_SECRET_KEY).strip(),
    "SDK_APP_ID": os.getenv("TENCENT_SMS_SDK_APP_ID", "").strip(),
    "SIGN_NAME": os.getenv("TENCENT_SMS_SIGN_NAME", "").strip(),
    "TEMPLATE_ID": os.getenv("TENCENT_SMS_TEMPLATE_ID", "").strip(),
    "REGION": os.getenv("TENCENT_SMS_REGION", "ap-guangzhou").strip(),
    "ENDPOINT": os.getenv("TENCENT_SMS_ENDPOINT", "sms.tencentcloudapi.com").strip(),
    "TIMEOUT_SECONDS": int(os.getenv("TENCENT_SMS_TIMEOUT_SECONDS", "30")),
    "SESSION_PREFIX": os.getenv("TENCENT_SMS_SESSION_PREFIX", "touming").strip(),
}

CRONJOBS = [
    ('*/5 * * * *', 'apps.company.tasks.auto_update_financial_overview'),
]


# Channels 配置（WebSocket）
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')
REDIS_DB = os.getenv('REDIS_DB', '0')
REDIS_AUTH_PART = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
REDIS_URL = os.getenv('REDIS_URL', f"redis://{REDIS_AUTH_PART}{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
REDIS_CACHE_URL = os.getenv('REDIS_CACHE_URL', REDIS_URL)

# Django cache backend. Used by SMS/captcha tokens and request rate limits.
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "KEY_PREFIX": os.getenv("REDIS_CACHE_KEY_PREFIX", "touming"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": int(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "5")),
            "SOCKET_TIMEOUT": int(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
        },
    },
}

ASGI_APPLICATION = 'config.asgi.application'

# Channel Layer 配置（使用 Redis）
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            "hosts": [REDIS_URL],
            "capacity": 1500,  # 默认容量
            "expiry": 10,  # 消息过期时间（秒）
        },
    },
}

# Celery 配置
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', REDIS_URL)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Shanghai'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30分钟
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25分钟
CELERY_BEAT_SCHEDULE = {
    'company-verification-expire-initiating': {
        'task': 'apps.company.tasks.expire_stale_company_verifications',
        'schedule': 300.0,
        'kwargs': {'limit': 100},
    },
    'company-verification-reconcile-pending': {
        'task': 'apps.company.tasks.reconcile_pending_company_verifications',
        'schedule': 900.0,
        'kwargs': {'limit': 50},
    },
    'company-verification-expire-manual': {
        'task': 'apps.company.tasks.expire_manual_company_verifications',
        'schedule': 300.0,
        'kwargs': {'limit': 100},
    },
    'company-verification-reconcile-manual-evidence': {
        'task': 'apps.company.tasks.reconcile_manual_verification_evidence',
        'schedule': 60.0,
        'kwargs': {'limit': 50},
    },
    'company-verification-reconcile-documents': {
        'task': 'apps.company.tasks.reconcile_pending_verification_documents',
        'schedule': 60.0,
        'kwargs': {'limit': 20},
    },
    'company-verification-document-cleanups': {
        'task': 'apps.company.tasks.reconcile_verification_document_cleanups',
        'schedule': 60.0,
        'kwargs': {'limit': 20},
    },
    'company-verification-review-evidence-refreshes': {
        'task': 'apps.company.tasks.reconcile_review_evidence_refreshes',
        'schedule': 60.0,
        'kwargs': {'limit': 20},
    },
    'company-verification-notifications': {
        'task': 'apps.company.tasks.reconcile_verification_notifications',
        'schedule': 60.0,
        'kwargs': {'limit': 50},
    },
    'company-verification-retention-dry-run': {
        'task': 'apps.company.tasks.plan_verification_document_retention',
        'schedule': 86400.0,
        'kwargs': {'limit': 100},
    },
    'company-verification-job-health': {
        'task': 'apps.company.tasks.snapshot_verification_job_health',
        'schedule': 300.0,
    },
    'agent-runtime-memory-retry': {
        'task': 'apps.agent_runtime.memory.tasks.retry_failed_memory_jobs',
        'schedule': 300.0,
        'kwargs': {'limit': 100},
    },
    'agent-runtime-viking-provisioning-retry': {
        'task': 'apps.agent_runtime.retry_failed_viking_bindings',
        'schedule': 300.0,
        'kwargs': {'limit': 50},
    },
    'agent-runtime-sandbox-reaper': {
        'task': 'apps.agent_runtime.providers.sandbox.tasks.reap_expired_sandboxes',
        'schedule': 60.0,
        'kwargs': {'limit': AGSX_REAPER_BATCH_SIZE},
    },
    'agent-runtime-schedule-runner': {
        'task': 'apps.agent_runtime.tasks.run_due_agent_schedules',
        'schedule': 60.0,
        'kwargs': {'limit': 50},
    },
    'agent-runtime-knowledge-file-scanner': {
        'task': 'apps.agent_runtime.knowledge.tasks.enqueue_pending_knowledge_files',
        'schedule': 300.0,
        'kwargs': {'limit': 100},
    },
    'agent-runtime-knowledge-retry': {
        'task': 'apps.agent_runtime.knowledge.tasks.retry_failed_knowledge_jobs',
        'schedule': 300.0,
        'kwargs': {'limit': 100},
    },
    'agent-runtime-operational-alerts': {
        'task': 'apps.agent_runtime.alerts.dispatch_runtime_alerts',
        'schedule': 60.0,
    },
}

# Platform risk control. High-frequency counters use Django cache/Redis.
RISK_CONTROL_ENABLED = _env_bool("RISK_CONTROL_ENABLED", True)
RISK_CONTROL_FAIL_POLICY = os.getenv("RISK_CONTROL_FAIL_POLICY", "deny").strip().lower()
RISK_CONTROL_REDIS_PREFIX = os.getenv("RISK_CONTROL_REDIS_PREFIX", "touming:risk").strip()
RISK_CONTROL_HASH_SALT = os.getenv("RISK_CONTROL_HASH_SALT", SECRET_KEY)
RISK_CONTROL_DEFAULT_IP_LIMIT_PER_MINUTE = int(os.getenv("RISK_CONTROL_DEFAULT_IP_LIMIT_PER_MINUTE", "60"))
RISK_CONTROL_DEFAULT_USER_LIMIT_PER_MINUTE = int(os.getenv("RISK_CONTROL_DEFAULT_USER_LIMIT_PER_MINUTE", "120"))
RISK_CONTROL_SMS_IP_LIMIT_PER_HOUR = int(os.getenv("RISK_CONTROL_SMS_IP_LIMIT_PER_HOUR", "30"))
RISK_CONTROL_SMS_PHONE_LIMIT_PER_DAY = int(os.getenv("RISK_CONTROL_SMS_PHONE_LIMIT_PER_DAY", "10"))
RISK_CONTROL_LOGIN_FAILURE_LIMIT_PER_15M = int(os.getenv("RISK_CONTROL_LOGIN_FAILURE_LIMIT_PER_15M", "10"))
RISK_CONTROL_API_KEY_LIMIT_PER_MINUTE = int(os.getenv("RISK_CONTROL_API_KEY_LIMIT_PER_MINUTE", "120"))
RISK_CONTROL_RUNTIME_CALL_LIMIT_PER_MINUTE = int(os.getenv("RISK_CONTROL_RUNTIME_CALL_LIMIT_PER_MINUTE", "30"))
RISK_CONTROL_LOG_ALLOW_DECISIONS = _env_bool("RISK_CONTROL_LOG_ALLOW_DECISIONS", False)



