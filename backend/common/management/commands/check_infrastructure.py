from uuid import uuid4

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from config.celery import app
from evidence import s3


class Command(BaseCommand):
    help = "Read-only PostGIS/channel layer/broker/worker checks; optionally check private S3 access."

    def add_arguments(self, parser):
        parser.add_argument("--s3", action="store_true", help="Check bucket reachability; never upload/download media.")

    def handle(self, *args, **options):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT ST_DWithin(ST_SetSRID(ST_MakePoint(106, 10), 4326)::geography, "
                               "ST_SetSRID(ST_MakePoint(106.001, 10), 4326)::geography, 300)")
                if cursor.fetchone() != (True,):
                    raise ValueError
        except Exception:
            raise CommandError("PostGIS check failed; verify extension, TLS and connectivity.") from None
        self.stdout.write("PostGIS spatial query: ok")
        layer = get_channel_layer()
        try:
            async_to_sync(layer.group_send)(f"health.{uuid4().hex}", {"type": "health.check"})
            with app.connection_for_read() as broker:
                broker.ensure_connection(max_retries=1)
            replies = app.control.inspect(timeout=5).ping()
            if not replies or not any(reply.get("ok") == "pong" for reply in replies.values()):
                raise ValueError
        except Exception:
            raise CommandError("Redis/channel/broker/worker check failed.") from None
        finally:
            if hasattr(layer, "close_pools"):
                async_to_sync(layer.close_pools)()
        self.stdout.write("Channel layer, broker and Celery worker: ok")
        if options["s3"]:
            try:
                s3.check_bucket()
            except s3.StorageUnavailable:
                raise CommandError("S3 bucket check failed; verify region/role/private bucket.") from None
            self.stdout.write("S3 bucket access: ok (upload not tested)")
