from django.urls import path
from rest_framework.routers import SimpleRouter
from .views import CategoryViewSet, ConfigurationView, TeamViewSet, UserViewSet

router = SimpleRouter()
router.register("users", UserViewSet, basename="managed-user")
router.register("categories", CategoryViewSet, basename="managed-category")
router.register("teams", TeamViewSet, basename="managed-team")
urlpatterns = router.urls + [path("configuration/", ConfigurationView.as_view())]
