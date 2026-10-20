from django.urls import path
from . import views

urlpatterns = [
    path('news', views.collection),
    path('news/access', views.access),
    path('news/images', views.upload_image),
    path('news/<int:news_id>', views.detail),
]
