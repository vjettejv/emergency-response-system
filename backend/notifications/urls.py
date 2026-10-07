from django.urls import path
from .views import DetailView, InboxView, ReadView, UnreadView

urlpatterns = [
    path("notifications/", InboxView.as_view()),
    path("notifications/unread-count/", UnreadView.as_view()),
    path("notifications/read-all/", ReadView.as_view()),
    path("notifications/<int:pk>/", DetailView.as_view()),
    path("notifications/<int:pk>/read/", ReadView.as_view()),
]
