from django.urls import include, path
from common.health import health
from realtime.views import frontend, landing

urlpatterns = [
    path("", landing, name="landing"),
    path("login", frontend, name="login"),
    path("api/health/", health, name="health"),
    path("api/v1/admin/", include("operations.urls")),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/", include("incidents.urls")),
    path("api/v1/", include("dispatch.urls")),
    path("api/v1/", include("teams.urls")),
    path("api/v1/", include("evidence.urls")),
    path("api/v1/", include("notifications.urls")),
    path("realtime/", include("realtime.urls")),
]
