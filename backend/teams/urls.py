from django.urls import path
from .views import GPSUpdateView, TeamLocationsView, JourneyListView, JourneyDetailView, JourneyStopView, RouteView, AssignmentRouteView

app_name = "teams"
urlpatterns = [
    path("teams/me/location/", GPSUpdateView.as_view(), name="update-location"),
    path("teams/locations/", TeamLocationsView.as_view(), name="locations"),
    path("journeys/", JourneyListView.as_view(), name="journeys"),
    path("journeys/<int:pk>/", JourneyDetailView.as_view(), name="journey-detail"),
    path("teams/me/journey/stop/", JourneyStopView.as_view(), name="journey-stop"),
    path("routes/", RouteView.as_view(), name="route"),
    path("assignments/<int:pk>/route/", AssignmentRouteView.as_view(), name="assignment-route"),
]
