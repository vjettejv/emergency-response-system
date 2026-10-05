import base64
import hashlib
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from django.conf import settings
from django.contrib.gis.geos import Point
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from dispatch.models import Assignment
from evidence import s3
from evidence.models import MediaAsset
from evidence.services import create_upload
from incidents.models import Incident, IncidentCategory, IncidentReport
from teams.models import ResponseTeam


@override_settings(S3_BUCKET_NAME="synthetic-private-bucket")
class MediaTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="citizen")
        self.other = User.objects.create_user(username="other")
        self.manager = User.objects.create_user(username="manager", role=Role.DISPATCHER)
        self.category = IncidentCategory.objects.create(name="Synthetic", code="synthetic")
        self.report = IncidentReport.objects.create(reporter=self.user, category=self.category, location=Point(105,21,srid=4326), description="Synthetic")
        self.incident = Incident.objects.create(title="Synthetic", category=self.category, location=Point(105,21,srid=4326), status="verified")
        self.client.force_authenticate(self.user)
        self.sdk = boto3.client("s3", region_name="ap-southeast-1", aws_access_key_id="synthetic", aws_secret_access_key="synthetic", config=Config(signature_version="s3v4"))
        self.sdk_patch = patch("evidence.s3.client", return_value=self.sdk)
        self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)

    def payload(self, **changes):
        return {"capture_source": "camera", "captured_at": timezone.now().isoformat(), "report_id":self.report.pk, "filename":"scene.jpg", "content_type":"image/jpeg", "size_bytes":3,
                "checksum_sha256":base64.b64encode(hashlib.sha256(b"abc").digest()).decode(), **changes}

    def create(self):
        return create_upload(actor=self.user, data=self.payload())[0]

    def head(self, asset, **changes):
        return {"ContentLength":asset.size_bytes, "ContentType":asset.content_type, "ChecksumSHA256":asset.checksum_sha256,
                "Metadata":{"media-id":str(asset.pk)}, **changes}

    def test_valid_metadata_produces_private_conditional_signed_put(self):
        response = self.client.post("/api/v1/media/presign/",self.payload(),format="json")
        self.assertEqual(response.status_code,201,response.data)
        asset = MediaAsset.objects.get()
        self.assertEqual(asset.status,"pending")
        self.assertEqual(asset.object_key,f"evidence/reports/{self.report.pk}/{asset.pk.hex}")
        upload=response.data["upload"]
        self.assertEqual(upload["headers"]["If-None-Match"],"*")
        self.assertNotIn("x-amz-acl",upload["headers"])
        signed=parse_qs(urlparse(upload["url"]).query)["X-Amz-SignedHeaders"][0]
        for name in ["content-length","content-type","if-none-match","x-amz-checksum-sha256","x-amz-meta-media-id"]:
            self.assertIn(name,signed)
        self.assertEqual(response["Cache-Control"],"no-store")

    def test_invalid_type_extension_size_checksum_and_injected_key(self):
        for change in [{"filename":"x.svg","content_type":"image/svg+xml"},{"filename":"x.exe"},
                       {"filename":"../x.jpg"},{"filename":"a\\x.jpg"},{"filename":"x.png"},
                       {"size_bytes":0},{"size_bytes":settings.MEDIA_MAX_BYTES+1},{"checksum_sha256":"bad"},
                       {"object_key":"other/key"},{"incident_id":self.incident.pk}]:
            response=self.client.post("/api/v1/media/presign/",self.payload(**change),format="json")
            self.assertEqual(response.status_code,400,change)
        self.assertFalse(MediaAsset.objects.exists())

    def test_ownership_and_review_state_enforced(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post("/api/v1/media/presign/",self.payload(),format="json").status_code,404)
        self.assertEqual(self.client.get(f"/api/v1/media/?report_id={self.report.pk}").status_code,404)
        self.client.force_authenticate(self.user)
        self.report.review_status="accepted"
        self.report.save()
        self.assertEqual(self.client.post("/api/v1/media/presign/",self.payload(),format="json").status_code,409)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/v1/media/?report_id={self.report.pk}").status_code,401)

    def test_confirm_checks_actual_object_and_is_idempotent(self):
        asset=self.create()
        with Stubber(self.sdk) as stub:
            stub.add_response("head_object",self.head(asset,VersionId="v1"),{"Bucket":asset.bucket,"Key":asset.object_key,"ChecksumMode":"ENABLED"})
            stub.add_response("put_object_tagging",{}, {"Bucket":asset.bucket,"Key":asset.object_key,"VersionId":"v1",
                "Tagging":{"TagSet":[{"Key":"upload-state","Value":"confirmed"}]}})
            response=self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json")
            self.assertEqual(response.status_code,200,response.data)
            self.assertEqual(response.data["status"],"ready")
            self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json").status_code,200)
            stub.assert_no_pending_responses()
        asset.refresh_from_db()
        self.assertEqual(asset.version_id,"v1")
        self.assertIsNotNone(asset.confirmed_at)
        response=self.client.get(f"/api/v1/media/{asset.pk}/download/")
        self.assertEqual(response.status_code,200)
        self.assertIn("versionId=v1",response.data["url"])
        self.assertIn("attachment",response.data["url"])

    def test_missing_object_or_mismatch_not_confirmed(self):
        asset=self.create()
        with Stubber(self.sdk) as stub:
            stub.add_client_error("head_object",service_error_code="404",http_status_code=404)
            self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json").status_code,409)
        for changes in [{"ContentLength":4},{"ContentType":"text/html"},{"Metadata":{}},{"ChecksumSHA256":"wrong"}]:
            with Stubber(self.sdk) as stub:
                stub.add_response("head_object",self.head(asset,**changes))
                self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json").status_code,400)
        asset.refresh_from_db()
        self.assertEqual(asset.status,"pending")
        self.assertEqual(self.client.get(f"/api/v1/media/{asset.pk}/download/").status_code,409)

    def test_expired_or_deleted_intent_and_unknown_confirm_fields_rejected(self):
        asset=self.create()
        self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{"object_key":"forged"},format="json").status_code,400)
        MediaAsset.objects.filter(pk=asset.pk).update(upload_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json").status_code,409)
        MediaAsset.objects.filter(pk=asset.pk).update(status="deleted")
        self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json").status_code,409)

    def test_s3_error_returns_503_without_changing_intent(self):
        asset=self.create()
        with patch.object(self.sdk,"head_object",side_effect=EndpointConnectionError(endpoint_url="https://synthetic.invalid")):
            response=self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json")
            self.assertEqual(response.status_code,503)
            self.assertNotIn("synthetic.invalid",str(response.data))
        with patch.object(self.sdk,"generate_presigned_url",side_effect=EndpointConnectionError(endpoint_url="https://synthetic.invalid")):
            self.assertEqual(self.client.post("/api/v1/media/presign/",self.payload(),format="json").status_code,503)
        self.assertEqual(MediaAsset.objects.count(),1)

    def test_invalid_stored_key_is_blocked_before_sdk_access(self):
        asset=self.create()
        MediaAsset.objects.filter(pk=asset.pk).update(object_key="../../private")
        self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json").status_code,503)

    def test_delete_is_durable_when_redis_unavailable_and_hides_download(self):
        asset=self.create()
        MediaAsset.objects.filter(pk=asset.pk).update(status="ready",confirmed_at=timezone.now())
        with patch("evidence.tasks.cleanup_media.apply_async",side_effect=ConnectionError("broker offline")) as queued:
            with self.assertLogs("evidence.tasks",level="WARNING"), self.captureOnCommitCallbacks(execute=True):
                response=self.client.delete(f"/api/v1/media/{asset.pk}/")
                self.assertEqual(response.status_code,202)
                queued.assert_not_called()
            queued.assert_called_once()
        asset.refresh_from_db()
        self.assertEqual(asset.status,"deleting")
        self.assertGreater(asset.next_cleanup_at,asset.upload_expires_at)
        self.assertEqual(self.client.get(f"/api/v1/media/{asset.pk}/download/").status_code,409)
        self.assertEqual(self.client.delete(f"/api/v1/media/{asset.pk}/").status_code,202)

    def test_media_list_does_not_duplicate_legacy_json_or_issue_download_urls(self):
        self.create()
        response=self.client.get(f"/api/v1/media/?report_id={self.report.pk}")
        self.assertEqual(response.data["count"],1)
        self.assertNotIn("object_key",response.data["results"][0])
        self.report.refresh_from_db()
        self.assertEqual(self.report.media_metadata,[])
        self.assertEqual(self.client.get("/api/v1/media/").status_code,400)

    def test_rescue_incident_access_requires_active_assignment_and_own_upload(self):
        team=ResponseTeam.objects.create(name="Synthetic",code="synthetic")
        rescue=User.objects.create_user(username="rescue",role=Role.RESCUE_TEAM,response_team=team)
        data=self.payload(); data.pop("report_id"); data["incident_id"]=self.incident.pk
        self.client.force_authenticate(rescue)
        self.assertEqual(self.client.post("/api/v1/media/presign/",data,format="json").status_code,404)
        assignment=Assignment.objects.create(incident=self.incident,team=team,assigned_by=self.manager)
        response=self.client.post("/api/v1/media/presign/",data,format="json")
        self.assertEqual(response.status_code,201,response.data)
        media_id=response.data["media"]["id"]
        self.assertEqual(self.client.get(f"/api/v1/media/?incident_id={self.incident.pk}").status_code,200)
        second=User.objects.create_user(username="member",role=Role.RESCUE_TEAM,response_team=team)
        self.client.force_authenticate(second)
        self.assertEqual(self.client.delete(f"/api/v1/media/{media_id}/").status_code,403)
        assignment.status="cancelled";assignment.ended_at=timezone.now();assignment.save()
        self.assertEqual(self.client.get(f"/api/v1/media/{media_id}/").status_code,404)
        self.client.force_authenticate(self.manager)
        self.incident.status="resolved";self.incident.save()
        self.assertEqual(self.client.post("/api/v1/media/presign/",data,format="json").status_code,409)

    @override_settings(S3_BUCKET_NAME="")
    def test_missing_storage_configuration_returns_503(self):
        self.sdk_patch.stop()
        self.assertEqual(self.client.post("/api/v1/media/presign/",self.payload(),format="json").status_code,503)

    def test_other_citizen_cannot_confirm_download_or_delete(self):
        asset=self.create()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(f"/api/v1/media/{asset.pk}/confirm/",{},format="json").status_code,404)
        self.assertEqual(self.client.get(f"/api/v1/media/{asset.pk}/download/").status_code,404)
        self.assertEqual(self.client.delete(f"/api/v1/media/{asset.pk}/").status_code,404)

    def test_sdk_cleanup_deletes_exact_version_and_missing_object_is_success(self):
        asset=self.create()
        with Stubber(self.sdk) as stub:
            stub.add_response("head_object",self.head(asset,VersionId="version-1"))
            stub.add_response("delete_object",{}, {"Bucket":asset.bucket,"Key":asset.object_key,"VersionId":"version-1"})
            s3.delete_object(asset)
            stub.add_client_error("head_object",service_error_code="404",http_status_code=404)
            s3.delete_object(asset)
            stub.assert_no_pending_responses()
