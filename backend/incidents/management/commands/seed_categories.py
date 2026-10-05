from django.core.management.base import BaseCommand
from django.db import transaction
from incidents.models import IncidentCategory


class Command(BaseCommand):
    help = "Initialize standard categories only; never create users, teams or incidents."

    @transaction.atomic
    def handle(self, *args, **options):
        created = 0
        for code, name in (("fire", "Cháy nổ"), ("traffic", "Tai nạn giao thông"),
                           ("medical", "Cấp cứu y tế"), ("flood", "Ngập lụt"),
                           ("other", "Sự cố khác")):
            _, added = IncidentCategory.objects.get_or_create(code=code, defaults={"name": name})
            created += added
        self.stdout.write(self.style.SUCCESS(f"Created {created} categories; existing records unchanged."))
