from django.urls import path
from .views import ConfirmView, DownloadView, MediaDetailView, MediaListView, PresignView

app_name = "evidence"
urlpatterns = [
    path("media/presign/", PresignView.as_view(), name="presign"),
    path("media/", MediaListView.as_view(), name="list"),
    path("media/<uuid:media_id>/", MediaDetailView.as_view(), name="detail"),
    path("media/<uuid:media_id>/confirm/", ConfirmView.as_view(), name="confirm"),
    path("media/<uuid:media_id>/download/", DownloadView.as_view(), name="download"),
]
