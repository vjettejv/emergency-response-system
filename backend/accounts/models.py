from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


class Role(models.TextChoices):
    CITIZEN = "citizen", "Citizen"
    DISPATCHER = "dispatcher", "Dispatcher"
    RESCUE_TEAM = "rescue_team", "Rescue Team"
    ADMIN = "admin", "Admin"


class AccountManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("role", Role.ADMIN)
        if extra_fields["role"] != Role.ADMIN:
            raise ValueError("A superuser must have the admin role.")
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CITIZEN)
    response_team = models.ForeignKey(
        "teams.ResponseTeam",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )
    objects = AccountManager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(role__in=Role.values), name="user_valid_role"),
            models.CheckConstraint(
                condition=models.Q(is_superuser=False) | models.Q(role=Role.ADMIN),
                name="superuser_has_admin_role",
            ),
            models.CheckConstraint(
                condition=models.Q(response_team__isnull=True) | models.Q(role=Role.RESCUE_TEAM),
                name="only_rescue_user_has_team",
            ),
        ]
