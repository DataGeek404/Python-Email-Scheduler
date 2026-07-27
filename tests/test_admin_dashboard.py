import importlib.util
import re
import tempfile
import unittest
from pathlib import Path


FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

if FLASK_AVAILABLE:
    from admin_dashboard import create_app


@unittest.skipUnless(FLASK_AVAILABLE, "Flask is installed by requirements.txt")
class AdminDashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = (Path(self.directory.name) / "dashboard.db").as_posix()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key",
                "ADMIN_PASSWORD": "correct-password",
                "DATABASE_URL": f"sqlite:///{path}",
                "SESSION_COOKIE_SECURE": False,
            }
        )
        self.client = self.app.test_client()
        self.store = self.app.extensions["monitoring_store"]

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def csrf_from(response):
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if not match:
            raise AssertionError("CSRF token not rendered")
        return match.group(1).decode()

    def login(self):
        login_page = self.client.get("/login")
        return self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_from(login_page),
                "username": "admin",
                "password": "correct-password",
            },
            follow_redirects=True,
        )

    def test_dashboard_requires_authentication(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_login_renders_complete_dashboard(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Operations overview", response.data)
        self.assertIn(b"Admin actions", response.data)
        self.assertIn(b"Event stream", response.data)

    def test_admin_action_is_queued_and_audited(self):
        self.login()
        with self.client.session_transaction() as user_session:
            csrf_token = user_session["csrf_token"]

        response = self.client.post(
            "/actions/check_smtp",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        command = self.store.list_commands(1)[0]
        self.assertEqual(command["action"], "check_smtp")
        self.assertEqual(command["status"], "queued")

    def test_admin_action_rejects_invalid_csrf(self):
        self.login()
        response = self.client.post(
            "/actions/send_now", data={"csrf_token": "invalid"}
        )
        self.assertEqual(response.status_code, 400)

    def test_health_endpoint_is_public(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["healthy"])

    def test_logs_can_be_filtered_and_downloaded(self):
        self.store.record_event("ERROR", "worker", "delivery failed")
        self.login()

        page = self.client.get("/logs?level=ERROR&search=delivery")
        csv_response = self.client.get("/logs.csv?level=ERROR")

        self.assertIn(b"delivery failed", page.data)
        self.assertEqual(csv_response.mimetype, "text/csv")
        self.assertIn(b"delivery failed", csv_response.data)


if __name__ == "__main__":
    unittest.main()
