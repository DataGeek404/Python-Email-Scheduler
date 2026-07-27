import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import daily_email_report as report
from monitoring import DatabaseLogHandler, MonitoringStore


class MonitoringStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = (Path(self.directory.name) / "monitor.db").as_posix()
        self.store = MonitoringStore(f"sqlite:///{path}")
        self.store.initialize()

    def tearDown(self):
        self.directory.cleanup()

    def test_healthcheck_and_scheduler_state(self):
        healthy, message = self.store.healthcheck()
        self.assertTrue(healthy)
        self.assertIn("sqlite", message)

        self.store.upsert_state(
            {
                "state": "running",
                "paused": False,
                "pid": 123,
                "next_run": "2026-07-28T08:30:00+03:00",
            }
        )

        state = self.store.get_state()
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["pid"], 123)

    def test_event_log_filtering_and_database_handler(self):
        self.store.record_event("INFO", "scheduler", "worker started")
        logger = logging.getLogger("test.monitoring")
        handler = DatabaseLogHandler(self.store)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.error("delivery failed")
        finally:
            logger.removeHandler(handler)

        events, total = self.store.list_events(level="ERROR", search="delivery")
        self.assertEqual(total, 1)
        self.assertEqual(events[0]["module"], "test.monitoring")
        self.assertIn("scheduler", self.store.event_modules())

    def test_delivery_history_and_stats(self):
        self.store.record_delivery(
            source="schedule",
            started_at="2026-07-27T10:00:00+00:00",
            status="success",
            to_count=1,
            cc_count=2,
            attachment="report.pdf",
        )

        deliveries = self.store.list_deliveries()
        stats = self.store.dashboard_stats()

        self.assertEqual(deliveries[0]["status"], "success")
        self.assertEqual(stats["total_24h"], 1)
        self.assertEqual(stats["success_24h"], 1)

    def test_command_queue_claim_and_completion(self):
        command_id = self.store.queue_command("check_smtp", "admin")

        commands = self.store.claim_commands()
        self.assertEqual([item["id"] for item in commands], [command_id])
        self.assertEqual(self.store.claim_commands(), [])

        self.store.finish_command(command_id, True, "SMTP ready")
        command = self.store.list_commands(1)[0]
        self.assertEqual(command["status"], "completed")
        self.assertEqual(command["result"], "SMTP ready")

    def test_rejects_unsupported_admin_action(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.store.queue_command("delete_everything", "admin")

    def test_queued_command_can_be_cancelled(self):
        command_id = self.store.queue_command("send_now", "admin")

        cancelled = self.store.cancel_command(command_id, "admin")

        self.assertTrue(cancelled)
        self.assertEqual(self.store.list_commands(1)[0]["status"], "cancelled")
        self.assertEqual(self.store.claim_commands(), [])

    def test_duplicate_pending_action_is_rejected(self):
        self.store.queue_command("send_now", "admin")

        with self.assertRaisesRegex(ValueError, "already"):
            self.store.queue_command("send_now", "admin")

    def test_worker_restart_marks_running_command_failed(self):
        self.store.queue_command("send_now", "admin")
        self.store.claim_commands()

        recovered = self.store.recover_running_commands()

        self.assertEqual(recovered, 1)
        command = self.store.list_commands(1)[0]
        self.assertEqual(command["status"], "failed")
        self.assertIn("verify delivery", command["result"])

    def worker_settings(self):
        attachment = Path(self.directory.name) / "report.pdf"
        attachment.write_bytes(b"%PDF-test")
        return report.Settings(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="sender@example.com",
            smtp_password="secret",
            from_email="sender@example.com",
            to_emails=("recipient@example.com",),
            cc_emails=(),
            subject="Report",
            body="Attached",
            attachment_path=attachment,
            schedule_time="08:30",
            smtp_retries=0,
            log_file=None,
            log_console=False,
        )

    def test_worker_executes_pause_command(self):
        self.store.queue_command("pause", "admin")

        with patch.object(report.LOGGER, "info"):
            paused, changed = report.process_admin_commands(
                self.worker_settings(), self.store, False
            )

        self.assertTrue(paused)
        self.assertTrue(changed)
        self.assertEqual(self.store.list_commands(1)[0]["status"], "completed")

    @patch("daily_email_report.deliver_and_record", return_value=True)
    def test_worker_executes_send_now_command(self, deliver):
        settings = self.worker_settings()
        self.store.queue_command("send_now", "admin")

        with patch.object(report.LOGGER, "info"):
            _paused, changed = report.process_admin_commands(
                settings, self.store, False
            )

        self.assertTrue(changed)
        deliver.assert_called_once_with(settings, self.store, source="dashboard")

    def test_scheduler_publishes_shared_worker_state(self):
        stop_event = Event()
        stop_event.set()
        settings = self.worker_settings()

        with patch.object(report.LOGGER, "info"):
            report.run_scheduler(settings, stop_event, self.store)

        state = self.store.get_state()
        self.assertEqual(state["state"], "stopped")
        self.assertIn("smtp.example.com", state["config_json"])


if __name__ == "__main__":
    unittest.main()
