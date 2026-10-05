from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_initial")]
    operations = [migrations.AddField(
        model_name="user", name="phone", field=models.CharField(blank=True, max_length=16),
    )]
