import os

os.environ.setdefault('DJANGO_ENVIRONMENT', 'production')

from .base import *  # noqa

DEBUG = False

# Admin安全配置 - 生产环境
ADMIN_ALLOWED_IPS = [
    '127.0.0.1',  # 本地
    '::1',        # IPv6本地
    # 添加你的服务器IP和管理员IP
    # '192.168.1.100',  # 示例：管理员IP
    # '10.0.0.0/8',     # 示例：内网段
]

# 安全设置
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# HTTPS重定向（如果使用HTTPS）
# SECURE_SSL_REDIRECT = True
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True

# Example: tighten ALLOWED_HOSTS in prod if needed
# ALLOWED_HOSTS = ["example.com"]

# Static files configuration for production
# STATIC_ROOT 必须与 STATICFILES_DIRS 不同
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
# 生产环境不需要 STATICFILES_DIRS（静态文件会被收集到 STATIC_ROOT）
STATICFILES_DIRS = []
