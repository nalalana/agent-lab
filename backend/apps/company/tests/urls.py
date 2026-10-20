from django.urls import include, path


urlpatterns = [
    path("api/company/", include("apps.company.urls")),
    path("api/runtime/", include("apps.runtime.urls")),
]
