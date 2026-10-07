import os

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Role, User
from incidents.models import IncidentCategory
from teams.models import ResponseTeam


class Command(BaseCommand):
    help = "Create development demo accounts and teams; preserve existing records/passwords."

    @transaction.atomic
    def handle(self, *args, **options):
        if settings.PRODUCTION:
            raise CommandError("Demo seeding is disabled in production. Use seed_categories instead.")
        password = os.environ.get("DEMO_PASSWORD", "")
        if not password:
            raise CommandError("Set DEMO_PASSWORD in the environment; it will not be printed.")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        category, _ = IncidentCategory.objects.get_or_create(code="fire", defaults={"name": "Cháy nổ"})
        traffic, _ = IncidentCategory.objects.get_or_create(code="traffic", defaults={"name": "Tai nạn giao thông"})
        team, created = ResponseTeam.objects.get_or_create(code="demo-rescue", defaults={
            "name": "Đội ứng cứu Demo", "status": ResponseTeam.Status.AVAILABLE,
            "last_location": Point(106.701, 10.776, srid=4326), "location_updated_at": timezone.now(),
        })
        if created:
            team.categories.add(category)
        if created or team.categories.filter(pk=category.pk).exists():
            team.categories.add(traffic)
        for role in Role.values:
            username = f"demo_{role}"
            if not User.objects.filter(username=username).exists():
                User.objects.create_user(username=username, password=password, role=role, response_team=team if role == Role.RESCUE_TEAM else None)
        for code, name, longitude, latitude, capability in (
            ("demo-medical", "Đội y tế Demo", 106.699, 10.779, traffic),
            ("demo-backup", "Đội hỗ trợ Demo", 106.705, 10.774, category),
        ):
            extra, new = ResponseTeam.objects.get_or_create(code=code, defaults={
                "name": name, "status": ResponseTeam.Status.AVAILABLE,
                "last_location": Point(longitude, latitude, srid=4326), "location_updated_at": timezone.now(),
            })
            if new:
                extra.categories.add(capability)
            if not User.objects.filter(username=code.replace("-", "_")).exists():
                User.objects.create_user(username=code.replace("-", "_"), password=password,
                                         role=Role.RESCUE_TEAM, response_team=extra)
        self.stdout.write(self.style.SUCCESS("Demo ready: demo_citizen, demo_dispatcher, demo_rescue_team, demo_medical, demo_backup, demo_admin. Existing data/passwords unchanged."))
