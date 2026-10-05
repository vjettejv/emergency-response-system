from django.urls import path

from .views import LoginView, LogoutView, MeView, RegisterView, UserRoleView

app_name = "accounts"
urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("users/<int:user_id>/role/", UserRoleView.as_view(), name="user-role"),
]
