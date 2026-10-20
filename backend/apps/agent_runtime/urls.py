"""URLs for unified Agent Runtime operational APIs."""

from django.urls import path

from apps.agent_runtime import views


urlpatterns = [
    path("runs", views.run_list, name="agent-runtime-run-list"),
    path("runs/<uuid:run_id>", views.run_detail, name="agent-runtime-run-detail"),
    path("runs/<uuid:run_id>/cancel", views.cancel_run, name="agent-runtime-run-cancel"),
    path("schedules", views.schedule_list, name="agent-runtime-schedule-list"),
    path("schedules/<uuid:schedule_id>", views.delete_schedule, name="agent-runtime-schedule-delete"),
    path("schedules/<uuid:schedule_id>/run", views.run_schedule_now, name="agent-runtime-schedule-run"),
    path("schedules/<uuid:schedule_id>/acknowledge", views.acknowledge_schedule, name="agent-runtime-schedule-acknowledge"),
    path("memory/jobs", views.memory_job_list, name="agent-runtime-memory-job-list"),
    path(
        "memory/jobs/<uuid:job_id>/retry",
        views.retry_memory_job,
        name="agent-runtime-memory-job-retry",
    ),
    path("knowledge/tree", views.knowledge_tree, name="agent-runtime-knowledge-tree"),
    path("knowledge/folders", views.knowledge_folder_create, name="agent-runtime-knowledge-folder-create"),
    path("knowledge/path/delete", views.knowledge_path_delete, name="agent-runtime-knowledge-path-delete"),
    path("knowledge/file", views.knowledge_file_content, name="agent-runtime-knowledge-file-content"),
    path("knowledge/upload", views.knowledge_upload, name="agent-runtime-knowledge-upload"),
    path("knowledge/files", views.knowledge_file_list, name="agent-runtime-knowledge-file-list"),
    path(
        "knowledge/files/<int:file_id>/reindex",
        views.reindex_knowledge_file,
        name="agent-runtime-knowledge-file-reindex",
    ),
    path(
        "migration/integrity",
        views.migration_integrity,
        name="agent-runtime-migration-integrity",
    ),
    path("metrics", views.metrics, name="agent-runtime-metrics"),
    path("alerts", views.runtime_alerts, name="agent-runtime-alerts"),
]
