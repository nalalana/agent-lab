from django.contrib import admin
from django.urls import path, include
from apps.opengate import views as opengate_views
from .health import health_check
from apps.company import views as company_views
from apps.company.integration_views import tencent_ess_callback
from apps.user import views as user_views
from apps.company.transparency_stats import transparency_stats
from apps.company.console_agents import console_agents
from apps.company.console_memory import console_memory

urlpatterns = [
    path('api/', include('apps.news.urls')),
    path("secret-admin-panel-2024/", admin.site.urls),
    path('health/', health_check, name='health_check'),
    path('captcha/', include('captcha.urls')),
    path('api/captcha/tencent/verify', user_views.verify_tencent_captcha),
    path('api/backend/transparency/stats', transparency_stats, name='backend-transparency-stats'),
    path('api/backend/console/agents', console_agents, name='backend-console-agents'),
    path('api/backend/console/memory', console_memory, name='backend-console-memory'),
    path('api/user/', include('apps.user.urls')),
    path('api/follow/', include('apps.follow.urls')),
    path('api/opengate/', include('apps.opengate.urls')),
    path('api/backend/tables/<str:table_id>/integrity', opengate_views.table_integrity, name='backend_tables_integrity'),
    path('api/backend/tables/<str:table_id>/rows/<str:row_id>/integrity', opengate_views.row_integrity, name='backend_tables_rows_integrity'),
    path('api/backend/tables/<str:table_id>/verify', opengate_views.verify_table_integrity, name='backend_tables_verify_integrity'),
    path('api/company/', include('apps.company.urls')),
    path(
        'integrations/tencent-ess/callback',
        tencent_ess_callback,
        name='tencent-ess-callback',
    ),
    path('api/agent-runtime/', include('apps.agent_runtime.urls')),
    path('api/backend/agents/<str:agent_id>/prompt-versions', company_views.transparent_company_agent_prompt_versions, name='backend-agent-prompt-versions'),
    path('api/runtime/', include('apps.runtime.urls')),
    path('api/ops/', include('apps.ops.urls')),
    path('api/iam/', include('apps.iam.urls')),
    path('api/notification/', include('apps.notification.urls')),
    path('api/sso/', include('apps.sso.urls')),
    path('api/risk-control/', include('apps.risk_control.urls')),
    path('api/moderation/', include('apps.moderation.urls')),
]


