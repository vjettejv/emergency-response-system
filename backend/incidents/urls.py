from rest_framework.routers import SimpleRouter
from django.urls import path
from .geocoding_views import GeocodingView

from .views import CategoryViewSet, IncidentViewSet, ReportViewSet

app_name = "incidents"
router = SimpleRouter()
router.register("incident-categories", CategoryViewSet, basename="category")
router.register("incident-reports", ReportViewSet, basename="report")
router.register("incidents", IncidentViewSet, basename="incident")
urlpatterns = router.urls + [
    path("geocoding/search/", GeocodingView.as_view(), {"operation": "search"}),
    path("geocoding/reverse/", GeocodingView.as_view(), {"operation": "reverse"}),
]
