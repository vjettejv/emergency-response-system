from rest_framework.routers import SimpleRouter

from .views import CategoryViewSet, IncidentViewSet, ReportViewSet

app_name = "incidents"
router = SimpleRouter()
router.register("incident-categories", CategoryViewSet, basename="category")
router.register("incident-reports", ReportViewSet, basename="report")
router.register("incidents", IncidentViewSet, basename="incident")
urlpatterns = router.urls
