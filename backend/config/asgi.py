"""
ASGI config for touming project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/asgi/
"""

import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

# 初始化 Django ASGI 应用
django_asgi_app = get_asgi_application()

# 注意：WebSocket 相关模块放到 Django 初始化之后再导入，
# 避免在 ASGI 模块导入阶段触发 Django models 导入导致 AppRegistryNotReady
from apps.notification.middleware import WebSocketTokenAuthMiddleware  # noqa: E402
from apps.notification import routing as notification_routing  # noqa: E402
from apps.opengate import routing as opengate_routing  # noqa: E402

# ASGI 应用配置
combined_patterns = notification_routing.websocket_urlpatterns + opengate_routing.websocket_urlpatterns

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": WebSocketTokenAuthMiddleware(
        URLRouter(
            combined_patterns
        )
    ),
})
