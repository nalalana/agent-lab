from django.urls import path
from apps.ops import views

urlpatterns = [
    path('list-hot-gate', views.list_hot_gate, name='list_hot_gate'),
    path('list-hot-company', views.list_hot_company, name='list_hot_company'),
    path('get-popular-gate', views.get_popular_gate, name='get_popular_gate'),
    path('get-popular-company', views.get_popular_company, name='get_popular_company'),
    path('get-partner', views.get_partner, name='get_partner'),
    path('set-popular-gate', views.set_popular_gate, name='set_popular_gate'),
    path('set-popular-company', views.set_popular_company, name='set_popular_company'),
    path('set-partner', views.set_partner, name='set_partner'),
]


