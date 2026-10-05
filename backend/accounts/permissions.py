from rest_framework.permissions import BasePermission

from .models import Role


class HasRole(BasePermission):
    allowed_roles = ()

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and user.is_active
            and user.role in self.allowed_roles
        )


class IsCitizen(HasRole):
    allowed_roles = (Role.CITIZEN,)


class IsDispatcher(HasRole):
    allowed_roles = (Role.DISPATCHER,)


class IsRescueTeam(HasRole):
    allowed_roles = (Role.RESCUE_TEAM,)


class IsAdmin(HasRole):
    allowed_roles = (Role.ADMIN,)


class IsDispatcherOrAdmin(HasRole):
    allowed_roles = (Role.DISPATCHER, Role.ADMIN)
