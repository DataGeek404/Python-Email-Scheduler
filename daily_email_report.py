"""Send an email report immediately or on a daily schedule."""

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Mapping, Sequence

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_BODY = (
    "Hello,\n\n"
    "Please find the daily report attached.\n\n"
    "Regards,\nAutomated Email Scheduler"
)
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
LOGGER = logging.getLogger("email_scheduler")


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str = field(repr=False)
    from_email: str
    to_emails: tuple[str, ...]
    cc_emails: tuple[str, ...]
    subject: str
    body: str
    attachment_path: Path
    schedule_time: str
    use_tls: bool = True
    smtp_timeout: float = 30.0


def configure_logging(log_file: Path | None = None) -> None:
    """Configure a file logger once without changing application-wide logging."""
    if any(isinstance(handler, logging.FileHandler) for handler in LOGGER.handlers):
        return

    path = log_file or Path(
        os.getenv("EMAIL_LOG_FILE", str(PROJECT_DIR / "email_scheduler.log"))
    )
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
    )
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _email_list(value: str | Sequence[str]) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value
    return tuple(
        address.strip()
        for item in values
        for address in item.split(",")
        if address.strip()
    )


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    """Build and validate settings from environment variables."""
    env = os.environ if environment is None else environment
    smtp_user = _required(env, "EMAIL_HOST_USER")
    attachment = Path(
        env.get("REPORT_ATTACHMENT", str(PROJECT_DIR / "Reports" / "jay.pdf"))
    ).expanduser()

    try:
        smtp_port = int(env.get("EMAIL_PORT", "587"))
        smtp_timeout = float(env.get("EMAIL_TIMEOUT", "30"))
    except ValueError as error:
        raise ConfigurationError(
            "EMAIL_PORT must be an integer and EMAIL_TIMEOUT must be a number"
        ) from error

    if not 1 <= smtp_port <= 65535:
        raise ConfigurationError("EMAIL_PORT must be between 1 and 65535")
    if smtp_timeout <= 0:
        raise ConfigurationError("EMAIL_TIMEOUT must be greater than zero")

    schedule_time = env.get("REPORT_TIME", "23:10").strip()
    if not TIME_PATTERN.fullmatch(schedule_time):
        raise ConfigurationError("REPORT_TIME must use 24-hour HH:MM format")

    to_emails = _email_list(_required(env, "EMAIL_TO"))
    if not to_emails:
        raise ConfigurationError("EMAIL_TO must contain at least one address")

    return Settings(
        smtp_host=env.get("EMAIL_HOST", "smtp.gmail.com").strip(),
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=_required(env, "EMAIL_HOST_PASSWORD"),
        from_email=env.get("EMAIL_FROM", smtp_user).strip(),
        to_emails=to_emails,
        cc_emails=_email_list(env.get("EMAIL_CC", "")),
        subject=env.get("EMAIL_SUBJECT", "Daily Report with Attachment"),
        body=env.get("EMAIL_BODY", DEFAULT_BODY),
        attachment_path=attachment,
        schedule_time=schedule_time,
        use_tls=_boolean(env.get("EMAIL_USE_TLS", "true"), "EMAIL_USE_TLS"),
        smtp_timeout=smtp_timeout,
    )


def build_message(
    *,
    from_email: str,
    to_emails: Sequence[str],
    cc_emails: Sequence[str],
    subject: str,
    body: str,
    attachment_path: Path,
) -> EmailMessage:
    """Create a complete email message, including the report attachment."""
    if not attachment_path.is_file():
        raise FileNotFoundError(f"Attachment not found: {attachment_path}")

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = ", ".join(to_emails)
    if cc_emails:
        message["Cc"] = ", ".join(cc_emails)
    message["Subject"] = subject
    message.set_content(body)

    mime_type, _ = mimetypes.guess_type(attachment_path.name)
    mime_type = mime_type or "application/octet-stream"
    main_type, sub_type = mime_type.split("/", 1)
    message.add_attachment(
        attachment_path.read_bytes(),
        maintype=main_type,
        subtype=sub_type,
        filename=attachment_path.name,
    )
    return message


def send_email_with_attachment(
    subject: str,
    body: str,
    to_email: str | Sequence[str],
    file_path: str | Path,
    cc_emails: str | Sequence[str] = (),
    *,
    settings: Settings | None = None,
) -> bool:
    """Send an attached email and return whether delivery was accepted by SMTP."""
    configure_logging()
    config = settings or load_settings()
    recipients = _email_list(to_email)
    cc_recipients = _email_list(cc_emails)

    try:
        message = build_message(
            from_email=config.from_email,
            to_emails=recipients,
            cc_emails=cc_recipients,
            subject=subject,
            body=body,
            attachment_path=Path(file_path),
        )
        all_recipients = [*recipients, *cc_recipients]

        with smtplib.SMTP(
            config.smtp_host, config.smtp_port, timeout=config.smtp_timeout
        ) as server:
            if config.use_tls:
                server.starttls(context=ssl.create_default_context())
            server.login(config.smtp_user, config.smtp_password)
            server.send_message(
                message,
                from_addr=config.from_email,
                to_addrs=all_recipients,
            )

        LOGGER.info(
            "Email sent successfully to=%s cc=%s attachment=%s",
            ",".join(recipients),
            ",".join(cc_recipients) or "none",
            file_path,
        )
        print(f"Email sent successfully to {', '.join(recipients)}")
        return True
    except (OSError, smtplib.SMTPException) as error:
        LOGGER.exception("Email delivery failed: %s", error)
        print(f"Email delivery failed: {error}")
        return False


def send_daily_report(settings: Settings | None = None) -> bool:
    """Send the report described by the current application settings."""
    config = settings or load_settings()
    return send_email_with_attachment(
        config.subject,
        config.body,
        config.to_emails,
        config.attachment_path,
        config.cc_emails,
        settings=config,
    )


def next_daily_run(schedule_time: str, now: datetime | None = None) -> datetime:
    """Return the next local date and time at which the report should run."""
    reference = now or datetime.now()
    hour, minute = (int(part) for part in schedule_time.split(":", 1))
    next_run = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= reference:
        next_run += timedelta(days=1)
    return next_run


def run_scheduler(settings: Settings) -> None:
    """Run the daily report worker until interrupted."""
    next_run = next_daily_run(settings.schedule_time)
    LOGGER.info(
        "Daily report scheduler started; send time=%s next_run=%s",
        settings.schedule_time,
        next_run.isoformat(timespec="minutes"),
    )
    print(
        "Daily report scheduler started; "
        f"next send: {next_run.isoformat(timespec='minutes')}"
    )

    while True:
        now = datetime.now()
        if now >= next_run:
            send_daily_report(settings)
            next_run = next_daily_run(settings.schedule_time)
            LOGGER.info("Next report scheduled for %s", next_run.isoformat())

        seconds_remaining = max((next_run - datetime.now()).total_seconds(), 0)
        time.sleep(min(seconds_remaining, 30))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="send one report immediately instead of starting the scheduler",
    )
    args = parser.parse_args()
    configure_logging()

    try:
        settings = load_settings()
        if args.send_now:
            return 0 if send_daily_report(settings) else 1
        run_scheduler(settings)
    except ConfigurationError as error:
        LOGGER.error("Configuration error: %s", error)
        print(f"Configuration error: {error}")
        return 2
    except KeyboardInterrupt:
        LOGGER.info("Daily report scheduler stopped")
        print("Daily report scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
