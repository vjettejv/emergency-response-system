from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIRequestFactory, APITestCase

from accounts.models import Role, User
from accounts.permissions import IsAdmin, IsCitizen, IsDispatcher, IsDispatcherOrAdmin, IsRescueTeam
from teams.models import ResponseTeam


class RolePermissionTests(SimpleTestCase):
    def test_role_matrix(self):
        permissions = {
            IsCitizen: {Role.CITIZEN},
            IsDispatcher: {Role.DISPATCHER},
            IsRescueTeam: {Role.RESCUE_TEAM},
            IsAdmin: {Role.ADMIN},
            IsDispatcherOrAdmin: {Role.DISPATCHER, Role.ADMIN},
        }
        request = APIRequestFactory().get("/")
        for permission, allowed_roles in permissions.items():
            for role in Role.values:
                with self.subTest(permission=permission.__name__, role=role):
                    request.user = User(username="sample", role=role)
                    self.assertEqual(permission().has_permission(request, None), role in allowed_roles)
            request.user = AnonymousUser()
            self.assertFalse(permission().has_permission(request, None))
            request.user = User(username="disabled", role=Role.ADMIN, is_active=False)
            self.assertFalse(permission().has_permission(request, None))


class AuthenticationTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_registration_cannot_set_privileged_fields(self):
        response = self.client.post(reverse("accounts:register"), {
            "username": "citizen1",
            "password": "Synthetic-report-pass-42!",
            "role": Role.ADMIN,
            "is_superuser": True,
            "is_staff": True,
        })
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(username="citizen1")
        self.assertEqual(user.role, Role.CITIZEN)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password("Synthetic-report-pass-42!"))
        self.assertNotIn("password", response.data)

    def test_registration_rejects_weak_password(self):
        response = self.client.post(reverse("accounts:register"), {
            "username": "citizen1", "password": "123",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_register_duplicate_username(self):
        User.objects.create_user(username="existing")
        response = self.client.post(reverse("accounts:register"), {
            "username": "existing", "password": "Synthetic-report-pass-42!",
        })
        self.assertEqual(response.status_code, 400)

    def test_login_me_logout_revokes_token(self):
        User.objects.create_user(username="citizen1", password="Synthetic-report-pass-42!")
        response = self.client.post(reverse("accounts:login"), {
            "username": "citizen1", "password": "Synthetic-report-pass-42!",
        })
        self.assertEqual(response.status_code, 200)
        token = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        response = self.client.get(reverse("accounts:me"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], Role.CITIZEN)
        self.assertEqual(self.client.post(reverse("accounts:logout")).status_code, 204)
        self.assertFalse(Token.objects.filter(key=token).exists())
        self.assertEqual(self.client.get(reverse("accounts:me")).status_code, 401)

    def test_anonymous_and_invalid_token_are_rejected(self):
        self.assertEqual(self.client.get(reverse("accounts:me")).status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION="Token invalid")
        self.assertEqual(self.client.get(reverse("accounts:me")).status_code, 401)

    def test_disabled_user_and_wrong_password_cannot_login(self):
        user = User.objects.create_user(username="citizen1", password="Synthetic-report-pass-42!")
        response = self.client.post(reverse("accounts:login"), {
            "username": user.username, "password": "wrong-password",
        })
        self.assertEqual(response.status_code, 400)
        token = Token.objects.create(user=user)
        user.is_active = False
        user.save(update_fields=["is_active"])
        response = self.client.post(reverse("accounts:login"), {
            "username": user.username, "password": "Synthetic-report-pass-42!",
        })
        self.assertEqual(response.status_code, 400)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(self.client.get(reverse("accounts:me")).status_code, 401)

    def test_only_admin_can_change_roles(self):
        target = User.objects.create_user(username="target")
        for role in Role.values:
            actor = User.objects.create_user(username=f"actor-{role}", role=role)
            self.client.force_authenticate(user=actor)
            response = self.client.patch(
                reverse("accounts:user-role", args=[target.pk]), {"role": Role.DISPATCHER},
            )
            with self.subTest(role=role):
                self.assertEqual(response.status_code, 200 if role == Role.ADMIN else 403)

    def test_role_changes_validate_values_and_clear_team(self):
        admin = User.objects.create_superuser(username="admin", password="Synthetic-admin-pass-42!")
        team = ResponseTeam.objects.create(name="Synthetic team", code="test-team")
        target = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM, response_team=team)
        self.client.force_authenticate(user=admin)
        url = reverse("accounts:user-role", args=[target.pk])
        self.assertEqual(self.client.patch(url, {"role": "invalid"}).status_code, 400)
        self.assertEqual(self.client.patch(url, {"role": Role.CITIZEN}).status_code, 200)
        target.refresh_from_db()
        self.assertIsNone(target.response_team_id)
        self.assertEqual(self.client.patch(
            reverse("accounts:user-role", args=[admin.pk]), {"role": Role.CITIZEN},
        ).status_code, 400)
