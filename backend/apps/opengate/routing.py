"""
/backend/apps/opengate/routing.py
WebSocket routing for table editing presence.
"""
from django.urls import re_path

from apps.opengate.consumers import TableEditingConsumer

websocket_urlpatterns = [
    re_path(r'^ws/tables/(?P<table_id>[^/]+)/edit/?$', TableEditingConsumer.as_asgi()),
]
