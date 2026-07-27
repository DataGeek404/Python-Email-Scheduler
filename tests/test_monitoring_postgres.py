import os
import unittest

from monitoring import MonitoringStore


POSTGRES_URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(POSTGRES_URL, "TEST_DATABASE_URL enables PostgreSQL integration tests")
class PostgreSQLMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.store = MonitoringStore(POSTGRES_URL)
        self.store.initialize()
        with self.store.connect() as connection:
            for table in (
                "admin_commands",
                "deliveries",
                "monitor_events",
                "scheduler_state",
            ):
                connection.execute(f"DELETE FROM {table}")

    def test_state_events_and_healthcheck(self):
        healthy, _message = self.store.healthcheck()
        self.assertTrue(healthy)

        self.store.upsert_state({"state": "running", "paused": False})
        self.store.record_event("INFO", "postgres-test", "connected")

        self.assertEqual(self.store.get_state()["state"], "running")
        self.assertEqual(self.store.list_events()[1], 1)

    def test_command_lifecycle(self):
        command_id = self.store.queue_command("check_config", "ci")
        claimed = self.store.claim_commands()
        self.store.finish_command(command_id, True, "valid")

        self.assertEqual(claimed[0]["id"], command_id)
        self.assertEqual(self.store.list_commands(1)[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
