from django.urls import path
from .views import frontend

urlpatterns = [path("", frontend), path("<str:asset>", frontend)]
