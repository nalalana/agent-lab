import os

os.environ.setdefault('DJANGO_ENVIRONMENT', 'development')

from .base import *  # noqa

DEBUG = os.getenv('DEBUG', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}

ALLOWED_HOSTS = ['*']
CORS_ORIGIN_ALLOW_ALL = True

BASE_URL = 'http://127.0.0.1:8000'
