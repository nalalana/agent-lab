from django.urls import path
from apps.follow import views

urlpatterns = [
    
    # Gate关注功能
    path('operation/gate', views.gate_operation),
    path('check/gate', views.check_gate_following_status),
    path('following/gates', views.get_following_gates),
    
    # Company关注功能
    path('operation/company', views.company_operation),
    path('check/company', views.check_company_following_status),
    path('following/companies', views.get_following_companies),
]
