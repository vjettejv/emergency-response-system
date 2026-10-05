from django.db import migrations


def retire_demo_categories(apps, schema_editor):
    Category = apps.get_model("incidents", "IncidentCategory")
    Team = apps.get_model("teams", "ResponseTeam")
    database = schema_editor.connection.alias
    for old_code, code, name in (
        ("demo-fire", "fire", "Cháy nổ"),
        ("demo-traffic", "traffic", "Tai nạn giao thông"),
    ):
        old = Category.objects.using(database).filter(code=old_code).first()
        if old is None:
            continue
        standard, _ = Category.objects.using(database).get_or_create(code=code, defaults={"name": name})
        # Preserve existing reports, incidents and team capabilities; stop new
        # submissions using the duplicate demo category, without deleting history.
        for team in Team.objects.using(database).filter(categories=old).iterator():
            team.categories.add(standard)
        Category.objects.using(database).filter(pk=old.pk).update(is_active=False, name=name)


class Migration(migrations.Migration):
    dependencies = [
        ("incidents", "0007_report_gps_location_and_contact_default"),
        ("teams", "0003_latest_gps_metadata"),
    ]
    operations = [migrations.RunPython(retire_demo_categories, migrations.RunPython.noop)]
