from django.contrib.gis.db import models
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("incidents", "0006_remove_incidentreport_report_cluster_time_idx_and_more")]
    operations = [
        migrations.AddField(model_name="incidentreport", name="gps_location",
                            field=models.PointField(blank=True, geography=True, null=True, srid=4326)),
        migrations.AlterField(model_name="incidentreport", name="allow_contact", field=models.BooleanField(default=True)),
    ]
