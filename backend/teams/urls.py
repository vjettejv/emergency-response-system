from django.urls import path
from .views import GPSUpdateView, TeamLocationsView

app_name = "teams"
urlpatterns = [
    path("teams/me/location/", GPSUpdateView.as_view(), name="update-location"),
    path("teams/locations/", TeamLocationsView.as_view(), name="locations"),
]
