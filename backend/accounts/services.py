from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import Role, User


@transaction.atomic
def change_role(*, actor, user_id, role):
    if not actor.is_active or actor.role != Role.ADMIN:
        raise PermissionDenied("Only an admin can change roles.")
    user = User.objects.select_for_update().get(pk=user_id)
    if role not in Role.values:
        raise ValidationError({"role": "Invalid role."})
    if user.is_superuser and role != Role.ADMIN:
        raise ValidationError({"role": "A superuser must retain the admin role."})
    if user.pk == actor.pk and role != Role.ADMIN:
        raise ValidationError({"role": "Admins cannot demote themselves through this endpoint."})
    user.role = role
    if role != Role.RESCUE_TEAM:
        user.response_team = None
    user.save(update_fields=["role", "response_team"])
    return user
