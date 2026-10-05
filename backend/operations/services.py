from django.contrib.gis.geos import Point
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError

from accounts.models import Role, User
from accounts.services import change_role
from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES, Assignment
from incidents.models import IncidentCategory
from incidents.services import Conflict, locked_object, require_role
from teams.models import ResponseTeam


@transaction.atomic
def save_user(*, actor, data, user_id=None):
    require_role(actor, (Role.ADMIN,))
    user = locked_object(User, user_id) if user_id else User()
    if (user.pk == actor.pk or user.is_superuser) and data.get("is_active") is False:
        raise ValidationError("Cannot disable yourself or a superuser.")
    password = data.pop("password", None)
    if user.pk and "role" in data:
        user = change_role(actor=actor, user_id=user.pk, role=data.pop("role"))
    for key, value in data.items():
        setattr(user, key, value)
    if user.role != Role.RESCUE_TEAM:
        user.response_team = None
    if password:
        user.set_password(password)
    user.full_clean(exclude=["password"])
    user.save()
    if password or not user.is_active:
        Token.objects.filter(user=user).delete()
    return user


@transaction.atomic
def save_team(*, actor, data, team_id=None):
    require_role(actor, (Role.ADMIN,))
    team = locked_object(ResponseTeam, team_id) if team_id else ResponseTeam()
    active = list(Assignment.objects.filter(team=team, status__in=ACTIVE_ASSIGNMENT_STATUSES)) if team.pk else []
    categories = data.pop("categories", None)
    if active:
        if data.get("status", team.status) != ResponseTeam.Status.BUSY or "latitude" in data:
            raise Conflict("Active teams must remain busy; GPS is updated by the rescue account.")
        if categories is not None and any(a.incident.category_id not in {c.pk for c in categories} for a in active):
            raise Conflict("Cannot remove a category supported by an active assignment.")
    elif data.get("status") == ResponseTeam.Status.BUSY:
        raise ValidationError({"status": "Busy is managed by assignments."})
    if "latitude" in data:
        team.last_location = Point(data.pop("longitude"), data.pop("latitude"), srid=4326)
        team.location_updated_at = timezone.now()
        team.location_received_at = timezone.now()
        team.location_accuracy = None
    for key, value in data.items():
        setattr(team, key, value)
    team.full_clean()
    team.save()
    if categories is not None:
        team.categories.set(categories)
    return team


@transaction.atomic
def delete_resource(*, actor, model, object_id):
    require_role(actor, (Role.ADMIN,))
    obj = locked_object(model, object_id)
    if model is ResponseTeam and obj.members.exists():
        raise Conflict("Remove team memberships before deleting this team; use offline to retain history.")
    try:
        obj.delete()
    except ProtectedError as exc:
        raise Conflict("This record is referenced by history. Deactivate it instead.") from exc


@transaction.atomic
def save_category(*, actor, data, category_id=None):
    require_role(actor, (Role.ADMIN,))
    category = locked_object(IncidentCategory, category_id) if category_id else IncidentCategory()
    for key, value in data.items():
        setattr(category, key, value)
    category.full_clean()
    category.save()
    return category
