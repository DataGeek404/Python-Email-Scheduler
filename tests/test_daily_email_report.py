import json
import smtplib
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import daily_email_report as report


def make_settings(attachment: Path, **overrides):
    defaults = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="sender@example.com",
        smtp_password="secret",
        from_email="sender@example.com",
        to_emails=("recipient@example.com",),
        cc_emails=("manager@example.com",),
        subject="Daily report",
        body="Attached.",
        attachment_path=attachment,
        schedule_time="08:05",
        smtp_retries=0,
        retry_base_seconds=0,
        log_file=None,
        log_console=False,
    )
    defaults.update(overrides)
    return report.Settings(**defaults)


class SettingsTests(unittest.TestCase):
    def base_environment(self):
        return {
            "EMAIL_HOST_USER": "sender@example.com",
            "EMAIL_HOST_PASSWORD": "secret",
            "EMAIL_TO": "first@example.com, second@example.com",
            "EMAIL_CC": "manager@example.com, first@example.com",
            "REPORT_TIME": "08:05",
        }

    def test_load_settings_validates_and_deduplicates_recipients(self):
        settings = report.load_settings(self.base_environment())

        self.assertEqual(
            settings.to_emails, ("first@example.com", "second@example.com")
        )
        self.assertEqual(settings.cc_emails, ("manager@example.com",))
        self.assertEqual(settings.schedule_time, "08:05")
        self.assertTrue(settings.attachment_path.is_absolute())
        self.assertNotIn("secret", repr(settings))

    def test_ssl_security_defaults_to_port_465(self):
        environment = self.base_environment()
        environment["EMAIL_SECURITY"] = "ssl"

        settings = report.load_settings(environment)

        self.assertEqual(settings.smtp_security, "ssl")
        self.assertEqual(settings.smtp_port, 465)

    def test_legacy_tls_switch_remains_supported(self):
        environment = self.base_environment()
        environment["EMAIL_USE_TLS"] = "false"

        settings = report.load_settings(environment)

        self.assertEqual(settings.smtp_security, "none")

    def test_load_settings_rejects_invalid_time(self):
        environment = self.base_environment()
        environment["REPORT_TIME"] = "25:00"
        with self.assertRaisesRegex(report.ConfigurationError, "HH:MM"):
            report.load_settings(environment)

    def test_load_settings_rejects_invalid_email(self):
        environment = self.base_environment()
        environment["EMAIL_TO"] = "not-an-address"
        with self.assertRaisesRegex(report.ConfigurationError, "invalid email"):
            report.load_settings(environment)

    def test_load_settings_rejects_invalid_security_mode(self):
        environment = self.base_environment()
        environment["EMAIL_SECURITY"] = "magic"
        with self.assertRaisesRegex(report.ConfigurationError, "starttls"):
            report.load_settings(environment)


class LoggingTests(unittest.TestCase):
    def test_configure_logging_writes_to_a_rotating_file(self):
        original_handlers = report.LOGGER.handlers[:]
        report.LOGGER.handlers.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                log_file = Path(directory) / "scheduler.log"
                attachment = Path(directory) / "report.pdf"
                settings = make_settings(
                    attachment,
                    log_file=log_file,
                    log_max_bytes=1024,
                    log_backup_count=1,
                )
                try:
                    report.configure_logging(settings=settings)
                    report.LOGGER.info("Email sent successfully")
                    for handler in report.LOGGER.handlers:
                        handler.flush()

                    self.assertIn(
                        "Email sent successfully", log_file.read_text("utf-8")
                    )
                finally:
                    for handler in report.LOGGER.handlers:
                        handler.close()
                    report.LOGGER.handlers.clear()
        finally:
            report.LOGGER.handlers[:] = original_handlers


class MessageTests(unittest.TestCase):
    def test_build_message_has_operational_headers_and_pdf_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "report.pdf"
            attachment.write_bytes(b"%PDF-test")

            message = report.build_message(
                from_email="sender@example.com",
                to_emails=("recipient@example.com",),
                cc_emails=("manager@example.com",),
                subject="Daily report",
                body="Attached.",
                attachment_path=attachment,
            )

        attached_parts = list(message.iter_attachments())
        self.assertEqual(message["Cc"], "manager@example.com")
        self.assertEqual(message["Auto-Submitted"], "auto-generated")
        self.assertIsNotNone(message["Date"])
        self.assertIsNotNone(message["Message-ID"])
        self.assertEqual(len(attached_parts), 1)
        self.assertEqual(attached_parts[0].get_content_type(), "application/pdf")
        self.assertEqual(attached_parts[0].get_filename(), "report.pdf")

    def test_build_message_rejects_oversized_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "report.pdf"
            attachment.write_bytes(b"too large")

            with self.assertRaisesRegex(report.AttachmentError, "configured limit"):
                report.build_message(
                    from_email="sender@example.com",
                    to_emails=("recipient@example.com",),
                    cc_emails=(),
                    subject="Daily report",
                    body="Attached.",
                    attachment_path=attachment,
                    max_attachment_bytes=2,
                )


class SchedulerTests(unittest.TestCase):
    def test_next_daily_run_is_today_when_time_is_still_upcoming(self):
        now = datetime(2026, 7, 27, 8, 4, 59)
        self.assertEqual(
            report.next_daily_run("08:05", now), datetime(2026, 7, 27, 8, 5)
        )

    def test_next_daily_run_is_tomorrow_after_time_has_passed(self):
        now = datetime(2026, 7, 27, 8, 5)
        self.assertEqual(
            report.next_daily_run("08:05", now), datetime(2026, 7, 28, 8, 5)
        )

    def test_next_daily_run_is_timezone_aware_for_utc(self):
        now = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
        next_run = report.next_daily_run("08:05", now, "UTC")
        self.assertEqual(next_run.utcoffset(), timedelta(0))
        self.assertEqual(next_run.hour, 8)

    def test_stopped_scheduler_writes_stopped_status(self):
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "report.pdf"
            attachment.write_bytes(b"%PDF-test")
            status_file = Path(directory) / "status.json"
            settings = make_settings(attachment, status_file=status_file)
            stop_event = Event()
            stop_event.set()

            with patch.object(report.LOGGER, "info"):
                report.run_scheduler(settings, stop_event)

            self.assertEqual(report.read_status(status_file)["state"], "stopped")


class StatusTests(unittest.TestCase):
    def test_status_health_accepts_fresh_running_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "status.json"
            report.write_status(status_file, state="running")

            healthy, message = report.status_is_healthy(status_file, 300)

            self.assertTrue(healthy)
            self.assertIn("heartbeat age", message)

    def test_status_health_rejects_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "status.json"
            payload = {
                "state": "running",
                "updated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            }
            status_file.write_text(json.dumps(payload), encoding="utf-8")

            healthy, message = report.status_is_healthy(status_file, 300)

            self.assertFalse(healthy)
            self.assertIn("old", message)


class CliTests(unittest.TestCase):
    @patch("daily_email_report.verify_smtp_connection", return_value=True)
    @patch("daily_email_report.configure_logging")
    @patch("daily_email_report.load_settings")
    def test_check_smtp_does_not_require_an_existing_attachment(
        self, load_settings, _configure_logging, verify_smtp
    ):
        load_settings.return_value = make_settings(Path("missing-report.pdf"))

        with patch.object(sys, "argv", ["daily_email_report.py", "--check-smtp"]):
            exit_code = report.main()

        self.assertEqual(exit_code, 0)
        verify_smtp.assert_called_once_with(load_settings.return_value)


class SendingTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.attachment = Path(self.temp_directory.name) / "report.pdf"
        self.attachment.write_bytes(b"%PDF-test")
        self.settings = make_settings(self.attachment)
        self.original_disabled = report.LOGGER.disabled
        report.LOGGER.disabled = True

    def tearDown(self):
        report.LOGGER.disabled = self.original_disabled
        self.temp_directory.cleanup()

    @staticmethod
    def successful_server():
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.return_value = {}
        server.noop.return_value = (250, b"OK")
        return server

    @patch("daily_email_report.ssl.create_default_context")
    @patch("daily_email_report.smtplib.SMTP")
    def test_send_uses_starttls_and_includes_cc_in_envelope(
        self, smtp_class, create_context
    ):
        server = self.successful_server()
        smtp_class.return_value = server

        sent = report.send_daily_report(self.settings)

        self.assertTrue(sent)
        smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=30.0)
        server.starttls.assert_called_once_with(context=create_context.return_value)
        server.login.assert_called_once_with("sender@example.com", "secret")
        _, keyword_args = server.send_message.call_args
        self.assertEqual(
            keyword_args["to_addrs"],
            ["recipient@example.com", "manager@example.com"],
        )

    @patch("daily_email_report.smtplib.SMTP_SSL")
    def test_ssl_mode_uses_smtp_ssl(self, smtp_ssl_class):
        server = self.successful_server()
        smtp_ssl_class.return_value = server
        settings = replace(self.settings, smtp_security="ssl", smtp_port=465)

        sent = report.send_daily_report(settings)

        self.assertTrue(sent)
        smtp_ssl_class.assert_called_once()
        server.starttls.assert_not_called()

    @patch("daily_email_report.time.sleep")
    @patch("daily_email_report.smtplib.SMTP")
    def test_transient_failure_retries_with_exponential_backoff(
        self, smtp_class, sleep
    ):
        server = self.successful_server()
        smtp_class.side_effect = [
            smtplib.SMTPServerDisconnected("temporary"),
            smtplib.SMTPServerDisconnected("temporary"),
            server,
        ]
        settings = replace(
            self.settings, smtp_retries=2, retry_base_seconds=0.5
        )

        sent = report.send_daily_report(settings)

        self.assertTrue(sent)
        self.assertEqual(smtp_class.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    @patch("daily_email_report.smtplib.SMTP")
    def test_authentication_failure_is_not_retried(self, smtp_class):
        server = self.successful_server()
        server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad login")
        smtp_class.return_value = server
        settings = replace(self.settings, smtp_retries=4)

        sent = report.send_daily_report(settings)

        self.assertFalse(sent)
        smtp_class.assert_called_once()

    @patch("daily_email_report.smtplib.SMTP")
    def test_partial_recipient_rejection_returns_false(self, smtp_class):
        server = self.successful_server()
        server.send_message.return_value = {
            "manager@example.com": (550, b"rejected")
        }
        smtp_class.return_value = server

        self.assertFalse(report.send_daily_report(self.settings))

    @patch("daily_email_report.smtplib.SMTP")
    def test_missing_attachment_does_not_connect(self, smtp_class):
        settings = replace(
            self.settings,
            attachment_path=Path(self.temp_directory.name) / "missing.pdf",
        )

        self.assertFalse(report.send_daily_report(settings))
        smtp_class.assert_not_called()

    @patch("daily_email_report.smtplib.SMTP")
    def test_smtp_health_check_authenticates_without_sending(self, smtp_class):
        server = self.successful_server()
        smtp_class.return_value = server

        self.assertTrue(report.verify_smtp_connection(self.settings))
        server.noop.assert_called_once_with()
        server.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
