import smtplib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import daily_email_report as report


class SettingsTests(unittest.TestCase):
    def test_load_settings_reads_recipients_and_cc(self):
        settings = report.load_settings(
            {
                "EMAIL_HOST_USER": "sender@example.com",
                "EMAIL_HOST_PASSWORD": "secret",
                "EMAIL_TO": "first@example.com, second@example.com",
                "EMAIL_CC": "manager@example.com, team@example.com",
                "REPORT_TIME": "08:05",
            }
        )

        self.assertEqual(
            settings.to_emails, ("first@example.com", "second@example.com")
        )
        self.assertEqual(
            settings.cc_emails, ("manager@example.com", "team@example.com")
        )
        self.assertEqual(settings.schedule_time, "08:05")
        self.assertNotIn("secret", repr(settings))

    def test_load_settings_rejects_invalid_time(self):
        with self.assertRaisesRegex(report.ConfigurationError, "HH:MM"):
            report.load_settings(
                {
                    "EMAIL_HOST_USER": "sender@example.com",
                    "EMAIL_HOST_PASSWORD": "secret",
                    "EMAIL_TO": "recipient@example.com",
                    "REPORT_TIME": "25:00",
                }
            )


class LoggingTests(unittest.TestCase):
    def test_configure_logging_writes_to_the_requested_file(self):
        original_handlers = report.LOGGER.handlers[:]
        report.LOGGER.handlers.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                log_file = Path(directory) / "scheduler.log"
                try:
                    report.configure_logging(log_file)
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
    def test_build_message_contains_cc_and_pdf_attachment(self):
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
        self.assertEqual(len(attached_parts), 1)
        self.assertEqual(attached_parts[0].get_content_type(), "application/pdf")
        self.assertEqual(attached_parts[0].get_filename(), "report.pdf")


class SchedulerTests(unittest.TestCase):
    def test_next_daily_run_is_today_when_time_is_still_upcoming(self):
        now = datetime(2026, 7, 27, 8, 4, 59)

        next_run = report.next_daily_run("08:05", now)

        self.assertEqual(next_run, datetime(2026, 7, 27, 8, 5))

    def test_next_daily_run_is_tomorrow_after_time_has_passed(self):
        now = datetime(2026, 7, 27, 8, 5)

        next_run = report.next_daily_run("08:05", now)

        self.assertEqual(next_run, datetime(2026, 7, 28, 8, 5))


class SendingTests(unittest.TestCase):
    def setUp(self):
        self.logging_patcher = patch("daily_email_report.configure_logging")
        self.logger_patcher = patch.object(report.LOGGER, "exception")
        self.output_patcher = patch("builtins.print")
        self.logging_patcher.start()
        self.logger_patcher.start()
        self.output_patcher.start()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.attachment = Path(self.temp_directory.name) / "report.pdf"
        self.attachment.write_bytes(b"%PDF-test")
        self.settings = report.Settings(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="sender@example.com",
            smtp_password="secret",
            from_email="sender@example.com",
            to_emails=("recipient@example.com",),
            cc_emails=("manager@example.com",),
            subject="Daily report",
            body="Attached.",
            attachment_path=self.attachment,
            schedule_time="08:05",
        )

    def tearDown(self):
        self.temp_directory.cleanup()
        self.output_patcher.stop()
        self.logger_patcher.stop()
        self.logging_patcher.stop()

    @patch("daily_email_report.ssl.create_default_context")
    @patch("daily_email_report.smtplib.SMTP")
    def test_send_uses_tls_and_delivers_to_primary_and_cc(
        self, smtp_class, create_context
    ):
        server = MagicMock()
        smtp_class.return_value.__enter__.return_value = server

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

    @patch("daily_email_report.smtplib.SMTP")
    def test_send_returns_false_when_smtp_fails(self, smtp_class):
        smtp_class.side_effect = smtplib.SMTPException("unavailable")

        sent = report.send_daily_report(self.settings)

        self.assertFalse(sent)

    @patch("daily_email_report.smtplib.SMTP")
    def test_missing_attachment_does_not_connect(self, smtp_class):
        missing_settings = report.Settings(
            **{
                **self.settings.__dict__,
                "attachment_path": Path(self.temp_directory.name) / "missing.pdf",
            }
        )

        sent = report.send_daily_report(missing_settings)

        self.assertFalse(sent)
        smtp_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
