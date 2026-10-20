from django.urls import path
from apps.sso import views

urlpatterns = [
    path('authorize', views.sso_authorize, name='sso-authorize'),
    path('exchange', views.sso_exchange_code, name='sso-exchange'),
    path('me', views.sso_me, name='sso-me'),
]
