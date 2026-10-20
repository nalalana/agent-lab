from django.urls import path

from apps.moderation import views


urlpatterns = [
    path("images/status", views.image_moderation_status, name="image-moderation-status"),
    path("images/review", views.image_moderation_manual_review, name="image-moderation-review"),
    path("cos/callback", views.image_moderation_cos_callback, name="image-moderation-cos-callback"),
    path("text/check", views.text_moderation_check, name="text-moderation-check"),
    path("text/status", views.text_moderation_status, name="text-moderation-status"),
    path("text/review", views.text_moderation_manual_review, name="text-moderation-review"),
]
