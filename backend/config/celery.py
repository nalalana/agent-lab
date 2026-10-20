"""
/backend/config/celery.py
Celery 配置
"""
import os
from celery import Celery

# 设置默认的 Django settings 模块
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')

app = Celery('touming')

# 从 Django settings 中加载配置
app.config_from_object('django.conf:settings', namespace='CELERY')

# 自动发现任务（从所有已安装的 app 中）
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    """调试任务"""
    print(f'Request: {self.request!r}')
