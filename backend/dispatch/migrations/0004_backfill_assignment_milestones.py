from django.db import migrations


def backfill(apps, schema_editor):
    Assignment = apps.get_model("dispatch", "Assignment")
    History = apps.get_model("dispatch", "AssignmentHistory")
    alias = schema_editor.connection.alias
    fields = {"accepted": "accepted_at", "en_route": "en_route_at",
              "on_scene": "arrived_at", "responding": "responding_at", "completed": "completed_at"}
    for assignment in Assignment.objects.using(alias).iterator():
        # Existing timestamps come only from recorded facts; never infer a responding step.
        values = {"dispatched_at": assignment.created_at}
        for history in History.objects.using(alias).filter(assignment_id=assignment.pk).order_by("created_at", "pk"):
            field = fields.get(history.to_status)
            if field and field not in values:
                values[field] = history.created_at
        Assignment.objects.using(alias).filter(pk=assignment.pk).update(**values)


class Migration(migrations.Migration):
    dependencies = [("dispatch", "0003_assignmentsignal_and_more")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
