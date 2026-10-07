from django.urls import path

from .consumers import DispatcherConsumer, RescueConsumer, NotificationConsumer

websocket_urlpatterns = [
    path("ws/dispatcher/", DispatcherConsumer.as_asgi()),
    path("ws/rescue/", RescueConsumer.as_asgi()),
    path("ws/notifications/", NotificationConsumer.as_asgi()),
]
