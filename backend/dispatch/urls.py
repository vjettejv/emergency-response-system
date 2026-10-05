from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import AssignmentViewSet, AssignTeamView, SuggestedTeamsView

app_name = "dispatch"
router = SimpleRouter()
router.register("assignments", AssignmentViewSet, basename="assignment")
urlpatterns = [
    path("incidents/<int:incident_id>/suggested-teams/", SuggestedTeamsView.as_view(), name="suggested-teams"),
    path("incidents/<int:incident_id>/assignments/", AssignTeamView.as_view(), name="assign-team"),
    *router.urls,
]
