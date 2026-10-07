from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.serializers import AuthTokenSerializer
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from common.throttling import SafeScopedRateThrottle
from rest_framework.views import APIView

from .models import User
from .permissions import IsAdmin
from .serializers import RegisterSerializer, RoleSerializer, UserSerializer
from .services import change_role


class PublicAuthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [SafeScopedRateThrottle]
    throttle_scope = "auth"


class RegisterView(PublicAuthView):
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(PublicAuthView):
    def post(self, request):
        serializer = AuthTokenSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": UserSerializer(user).data})


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class LogoutView(APIView):
    def post(self, request):
        request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserRoleView(APIView):
    permission_classes = [IsAdmin]

    def patch(self, request, user_id):
        serializer = RoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = change_role(actor=request.user, user_id=user_id, **serializer.validated_data)
        except User.DoesNotExist as exc:
            raise NotFound("User not found.") from exc
        return Response(UserSerializer(user).data)
