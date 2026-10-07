from django.test import SimpleTestCase
from django.urls import resolve


class PublicLandingTests(SimpleTestCase):
    def body(self, response):
        return b"".join(response.streaming_content).decode()

    def test_guest_page_is_public_static_and_has_no_operational_dependencies(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = self.body(response)
        for section in ["trang-chu", "tinh-nang", "quy-trinh", "gioi-thieu", "entry-title"]:
            self.assertIn(f'id="{section}"', content)
        self.assertIn('href="/login"', content)
        self.assertIn("nghiên cứu và thử nghiệm", content)
        for private_asset in ["app.js", "map.js", "leaflet", "style.css", "WebSocket(", "watchPosition", "getUserMedia"]:
            self.assertNotIn(private_asset, content)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_login_and_legacy_role_shell_still_use_existing_app(self):
        for path in ["/login", "/realtime/"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            content = self.body(response)
            self.assertIn('src="/realtime/app.js"', content)
            self.assertIn('href="/realtime/theme.css"', content)
        self.assertEqual(resolve("/api/v1/auth/login/").url_name, "login")

    def test_public_assets_are_allowlisted_and_have_correct_types(self):
        for asset, mime in [("landing.css", "text/css"), ("theme.css", "text/css"), ("landing.js", "text/javascript"), ("landing-map.svg", "image/svg+xml")]:
            response = self.client.get("/realtime/" + asset)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], mime)
            response.close()
        self.assertEqual(self.client.get("/realtime/private.env").status_code, 404)
        self.assertEqual(self.client.post("/").status_code, 405)
        self.assertEqual(self.client.post("/login").status_code, 405)
