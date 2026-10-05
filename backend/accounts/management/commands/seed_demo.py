import os

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
    help = "Create synthetic demo accounts and an available team; preserve existing records/passwords."

    @transaction.atomic
    def handle(self, *args, **options):
        password = os.environ.get("DEMO_PASSWORD", "")
        if not password:
            raise CommandError("Set DEMO_PASSWORD in the environment; it will not be printed.")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        category, _ = IncidentCategory.objects.get_or_create(code="demo-fire", defaults={"name": "Cháy / khói (demo)", "description": "Dữ liệu mô phỏng, không phải sự cố thật."})
        traffic, _ = IncidentCategory.objects.get_or_create(code="demo-traffic", defaults={"name": "Tai nạn giao thông (demo)", "description": "Dữ liệu mô phỏng, không phải sự cố thật."})
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
        self.stdout.write(self.style.SUCCESS("Demo ready: demo_citizen, demo_dispatcher, demo_rescue_team, demo_admin. Existing data/passwords unchanged."))
