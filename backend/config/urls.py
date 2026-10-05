from django.urls import include, path
from django.views.generic import RedirectView
from common.health import health

urlpatterns = [
    path("", RedirectView.as_view(url="/realtime/", permanent=False)),
    path("api/health/", health, name="health"),
    path("api/v1/admin/", include("operations.urls")),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/", include("incidents.urls")),
    path("api/v1/", include("dispatch.urls")),
    path("api/v1/", include("teams.urls")),
    path("api/v1/", include("evidence.urls")),
    path("realtime/", include("realtime.urls")),
]
