from django.urls import path

from apps.risk_control import views


urlpatterns = [
    path("logs", views.decision_logs, name="risk-control-logs"),
    path("logs/", views.decision_logs),
    path("entries", views.list_entries, name="risk-control-entries"),
    path("entries/", views.list_entries),
    path("manual-action", views.manual_action, name="risk-control-manual-action"),
    path("manual-action/", views.manual_action),
]
