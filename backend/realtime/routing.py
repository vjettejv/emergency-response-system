from django.urls import path

from .consumers import DispatcherConsumer, RescueConsumer

websocket_urlpatterns = [
    path("ws/dispatcher/", DispatcherConsumer.as_asgi()),
    path("ws/rescue/", RescueConsumer.as_asgi()),
]
