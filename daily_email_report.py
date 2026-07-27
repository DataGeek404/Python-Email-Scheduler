"""Production-ready daily email report worker."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import signal
import smtplib
import socket
import ssl
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_BODY = (
    "Hello,\n\n"
    "Please find the daily report attached.\n\n"
    "Regards,\nAutomated Email Scheduler"
)
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
VALID_SECURITY_MODES = {"starttls", "ssl", "none"}
LOGGER = logging.getLogger("email_scheduler")


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


class AttachmentError(OSError):
    """Raised when a report attachment cannot safely be sent."""


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
    smtp_security: str = "starttls"
    smtp_timeout: float = 30.0
    smtp_retries: int = 2
    retry_base_seconds: float = 2.0
    max_attachment_bytes: int = 18 * 1024 * 1024
    timezone_name: str = "local"
    poll_seconds: float = 30.0
    heartbeat_seconds: float = 60.0
    status_file: Path = field(
        default_factory=lambda: PROJECT_DIR / "email_scheduler_status.json"
    )
    log_file: Path | None = field(
        default_factory=lambda: PROJECT_DIR / "email_scheduler.log"
    )
    log_level: str = "INFO"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3
    log_console: bool = True

    @property
    def use_tls(self) -> bool:
        """Backward-compatible indicator for STARTTLS mode."""
        return self.smtp_security == "starttls"


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _integer(
    environment: Mapping[str, str], name: str, default: int, minimum: int
) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _number(
    environment: Mapping[str, str], name: str, default: float, minimum: float
) -> float:
    try:
        value = float(environment.get(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a number") from error
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _project_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve(strict=False)


def _email_list(
    value: str | Sequence[str], name: str = "email list"
) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value
    addresses: list[str] = []
    seen: set[str] = set()
    for item in values:
        for raw_address in item.split(","):
            address = raw_address.strip()
            if not address:
                continue
            try:
                parsed = Address(addr_spec=address)
            except (TypeError, ValueError) as error:
                raise ConfigurationError(
                    f"{name} contains an invalid email address: {address!r}"
                ) from error
            if not parsed.username or not parsed.domain:
                raise ConfigurationError(
                    f"{name} contains an invalid email address: {address!r}"
                )
            key = address.casefold()
            if key not in seen:
                seen.add(key)
                addresses.append(address)
    return tuple(addresses)


def timezone_from_name(name: str):
    """Resolve an IANA timezone or the host's local timezone."""
    normalized = name.casefold()
    if normalized == "local":
        return datetime.now().astimezone().tzinfo
    if normalized in {"utc", "etc/utc", "gmt"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ConfigurationError(
            f"REPORT_TIMEZONE is unknown: {name!r}; use an IANA name or 'local'"
        ) from error


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    """Build and validate immutable settings from environment variables."""
    env = os.environ if environment is None else environment
    smtp_user = _required(env, "EMAIL_HOST_USER")
    smtp_password = _required(env, "EMAIL_HOST_PASSWORD")

    security = env.get("EMAIL_SECURITY", "").strip().lower()
    if not security:
        security = (
            "starttls"
            if _boolean(env.get("EMAIL_USE_TLS", "true"), "EMAIL_USE_TLS")
            else "none"
        )
    if security not in VALID_SECURITY_MODES:
        raise ConfigurationError("EMAIL_SECURITY must be starttls, ssl, or none")

    default_port = 465 if security == "ssl" else 587
    smtp_port = _integer(env, "EMAIL_PORT", default_port, 1)
    if smtp_port > 65535:
        raise ConfigurationError("EMAIL_PORT must not exceed 65535")

    schedule_time = env.get("REPORT_TIME", "23:10").strip()
    if not TIME_PATTERN.fullmatch(schedule_time):
        raise ConfigurationError("REPORT_TIME must use 24-hour HH:MM format")

    timezone_name = env.get("REPORT_TIMEZONE", "local").strip() or "local"
    timezone_from_name(timezone_name)

    to_emails = _email_list(_required(env, "EMAIL_TO"), "EMAIL_TO")
    if not to_emails:
        raise ConfigurationError("EMAIL_TO must contain at least one address")
    cc_emails = _email_list(env.get("EMAIL_CC", ""), "EMAIL_CC")
    to_keys = {address.casefold() for address in to_emails}
    cc_emails = tuple(
        address for address in cc_emails if address.casefold() not in to_keys
    )

    from_addresses = _email_list(
        env.get("EMAIL_FROM", smtp_user).strip(), "EMAIL_FROM"
    )
    if len(from_addresses) != 1:
        raise ConfigurationError("EMAIL_FROM must contain exactly one address")
    from_email = from_addresses[0]

    log_level = env.get("EMAIL_LOG_LEVEL", "INFO").strip().upper()
    if not isinstance(logging.getLevelName(log_level), int):
        raise ConfigurationError(f"EMAIL_LOG_LEVEL is invalid: {log_level!r}")

    raw_log_file = env.get("EMAIL_LOG_FILE", "email_scheduler.log").strip()
    log_file = (
        None
        if raw_log_file.casefold() in {"", "none", "-"}
        else _project_path(raw_log_file)
    )

    return Settings(
        smtp_host=env.get("EMAIL_HOST", "smtp.gmail.com").strip()
        or "smtp.gmail.com",
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        from_email=from_email,
        to_emails=to_emails,
        cc_emails=cc_emails,
        subject=env.get("EMAIL_SUBJECT", "Daily Report with Attachment"),
        body=env.get("EMAIL_BODY", DEFAULT_BODY),
        attachment_path=_project_path(
            env.get("REPORT_ATTACHMENT", "Reports/jay.pdf")
        ),
        schedule_time=schedule_time,
        smtp_security=security,
        smtp_timeout=_number(env, "EMAIL_TIMEOUT", 30.0, 1.0),
        smtp_retries=_integer(env, "EMAIL_RETRIES", 2, 0),
        retry_base_seconds=_number(env, "EMAIL_RETRY_BASE_SECONDS", 2.0, 0.0),
        max_attachment_bytes=_integer(
            env, "EMAIL_MAX_ATTACHMENT_MB", 18, 1
        )
        * 1024
        * 1024,
        timezone_name=timezone_name,
        poll_seconds=_number(env, "SCHEDULER_POLL_SECONDS", 30.0, 1.0),
        heartbeat_seconds=_number(
            env, "SCHEDULER_HEARTBEAT_SECONDS", 60.0, 1.0
        ),
        status_file=_project_path(
            env.get("EMAIL_STATUS_FILE", "email_scheduler_status.json")
        ),
        log_file=log_file,
        log_level=log_level,
        log_max_bytes=_integer(env, "EMAIL_LOG_MAX_MB", 5, 1) * 1024 * 1024,
        log_backup_count=_integer(env, "EMAIL_LOG_BACKUP_COUNT", 3, 0),
        log_console=_boolean(
            env.get("EMAIL_LOG_CONSOLE", "true"), "EMAIL_LOG_CONSOLE"
        ),
    )


def configure_logging(
    log_file: Path | None = None, *, settings: Settings | None = None
) -> None:
    """Configure rotating file and console logs once."""
    if any(
        getattr(handler, "_email_scheduler_managed", False)
        for handler in LOGGER.handlers
    ):
        return

    chosen_file = settings.log_file if settings is not None else (
        log_file or PROJECT_DIR / "email_scheduler.log"
    )
    log_level = settings.log_level if settings is not None else "INFO"
    max_bytes = settings.log_max_bytes if settings is not None else 5 * 1024 * 1024
    backup_count = settings.log_backup_count if settings is not None else 3
    use_console = settings.log_console if settings is not None else True
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s process=%(process)d %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime

    LOGGER.setLevel(log_level)
    LOGGER.propagate = False

    if chosen_file is not None:
        try:
            chosen_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                chosen_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler._email_scheduler_managed = True  # type: ignore[attr-defined]
            LOGGER.addHandler(file_handler)
        except OSError as error:
            use_console = True
            print(f"Unable to open log file {chosen_file}: {error}", file=sys.stderr)

    if use_console or not LOGGER.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler._email_scheduler_managed = True  # type: ignore[attr-defined]
        LOGGER.addHandler(console_handler)


def validate_attachment(path: Path, max_bytes: int) -> int:
    """Validate attachment availability and size, returning its byte size."""
    if not path.is_file():
        raise AttachmentError(f"Attachment not found: {path}")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise AttachmentError(f"Unable to inspect attachment {path}: {error}") from error
    if size > max_bytes:
        raise AttachmentError(
            f"Attachment is {size / 1024 / 1024:.1f} MB; configured limit is "
            f"{max_bytes / 1024 / 1024:.1f} MB"
        )
    return size


def build_message(
    *,
    from_email: str,
    to_emails: Sequence[str],
    cc_emails: Sequence[str],
    subject: str,
    body: str,
    attachment_path: Path,
    max_attachment_bytes: int = 18 * 1024 * 1024,
    message_id: str | None = None,
) -> EmailMessage:
    """Create a standards-compliant message with a validated attachment."""
    validate_attachment(attachment_path, max_attachment_bytes)
    try:
        attachment_data = attachment_path.read_bytes()
    except OSError as error:
        raise AttachmentError(f"Unable to read attachment {attachment_path}: {error}") from error
    if len(attachment_data) > max_attachment_bytes:
        raise AttachmentError("Attachment grew beyond the configured limit while reading")

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = ", ".join(to_emails)
    if cc_emails:
        message["Cc"] = ", ".join(cc_emails)
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    domain = from_email.rsplit("@", 1)[-1]
    message["Message-ID"] = message_id or make_msgid(domain=domain)
    message["Auto-Submitted"] = "auto-generated"
    message["Precedence"] = "bulk"
    message.set_content(body)

    mime_type, _ = mimetypes.guess_type(attachment_path.name)
    mime_type = mime_type or "application/octet-stream"
    main_type, sub_type = mime_type.split("/", 1)
    message.add_attachment(
        attachment_data,
        maintype=main_type,
        subtype=sub_type,
        filename=attachment_path.name,
    )
    return message


def _open_smtp(settings: Settings):
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout,
            context=context,
        )
    else:
        server = smtplib.SMTP(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout,
        )
    try:
        server.ehlo()
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        server.login(settings.smtp_user, settings.smtp_password)
        return server
    except Exception:
        server.close()
        raise


@contextmanager
def _smtp_session(settings: Settings):
    """Close SMTP cleanly without treating a post-delivery QUIT error as failure."""
    server = _open_smtp(settings)
    try:
        yield server
    finally:
        try:
            server.quit()
        except (OSError, smtplib.SMTPException):
            server.close()


def _retryable_smtp_error(error: BaseException) -> bool:
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [response[0] for response in error.recipients.values()]
        return bool(codes) and all(400 <= code < 500 for code in codes)
    if isinstance(error, smtplib.SMTPResponseException):
        return 400 <= error.smtp_code < 500
    if isinstance(error, (smtplib.SMTPServerDisconnected, OSError)):
        return True
    return isinstance(error, smtplib.SMTPException)


def verify_smtp_connection(settings: Settings) -> bool:
    """Authenticate to SMTP without sending a message."""
    try:
        with _smtp_session(settings) as server:
            code, _ = server.noop()
        if code != 250:
            LOGGER.error("SMTP health check returned status=%s", code)
            return False
        LOGGER.info(
            "SMTP authentication succeeded host=%s port=%s security=%s",
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_security,
        )
        return True
    except (OSError, smtplib.SMTPException) as error:
        LOGGER.error("SMTP authentication failed: %s", error)
        return False


def send_email_with_attachment(
    subject: str,
    body: str,
    to_email: str | Sequence[str],
    file_path: str | Path,
    cc_emails: str | Sequence[str] = (),
    *,
    settings: Settings | None = None,
) -> bool:
    """Send a report with bounded retries and return complete-delivery status."""
    config = settings or load_settings()
    configure_logging(settings=config)

    try:
        recipients = _email_list(to_email, "To recipients")
        cc_recipients = _email_list(cc_emails, "CC recipients")
        if not recipients:
            raise ConfigurationError("At least one To recipient is required")
        message = build_message(
            from_email=config.from_email,
            to_emails=recipients,
            cc_emails=cc_recipients,
            subject=subject,
            body=body,
            attachment_path=Path(file_path),
            max_attachment_bytes=config.max_attachment_bytes,
        )
    except (ConfigurationError, AttachmentError, OSError) as error:
        LOGGER.error("Email preparation failed: %s", error)
        return False

    all_recipients = [*recipients, *cc_recipients]
    attempts = config.smtp_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            with _smtp_session(config) as server:
                refused = server.send_message(
                    message,
                    from_addr=config.from_email,
                    to_addrs=all_recipients,
                )
            if refused:
                LOGGER.error(
                    "Email was only partially accepted; refused=%s",
                    ",".join(sorted(refused)),
                )
                return False

            LOGGER.info(
                "Email sent successfully to=%s cc=%s attachment=%s attempt=%s",
                ",".join(recipients),
                ",".join(cc_recipients) or "none",
                file_path,
                attempt,
            )
            return True
        except (OSError, smtplib.SMTPException) as error:
            if attempt >= attempts or not _retryable_smtp_error(error):
                LOGGER.exception(
                    "Email delivery failed attempt=%s/%s: %s",
                    attempt,
                    attempts,
                    error,
                )
                return False
            delay = config.retry_base_seconds * (2 ** (attempt - 1))
            LOGGER.warning(
                "Transient SMTP error attempt=%s/%s; retrying in %.1fs: %s",
                attempt,
                attempts,
                delay,
                error,
            )
            time.sleep(delay)
    return False


def send_daily_report(settings: Settings | None = None) -> bool:
    """Send the report described by application settings."""
    config = settings or load_settings()
    return send_email_with_attachment(
        config.subject,
        config.body,
        config.to_emails,
        config.attachment_path,
        config.cc_emails,
        settings=config,
    )


def next_daily_run(
    schedule_time: str,
    now: datetime | None = None,
    timezone_name: str = "local",
) -> datetime:
    """Return the next occurrence of HH:MM in the configured timezone."""
    is_local = timezone_name.casefold() == "local"
    tz = timezone_from_name(timezone_name)
    if now is None:
        reference = datetime.now() if is_local else datetime.now(tz)
    elif now.tzinfo is None:
        reference = (
            now
            if is_local
            else now.replace(tzinfo=tz)
        )
    else:
        reference = now.astimezone(tz)

    hour, minute = (int(part) for part in schedule_time.split(":", 1))
    next_run = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= reference:
        next_run += timedelta(days=1)
    return next_run


def _status_payload(**values: Any) -> dict[str, Any]:
    return {
        **values,
        "pid": os.getpid(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_status(path: Path, **values: Any) -> None:
    """Atomically write scheduler health information for process monitors."""
    payload = _status_payload(**values)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as error:
        LOGGER.warning("Unable to update status file %s: %s", path, error)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_status(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Unable to read status file {path}: {error}") from error


def status_is_healthy(path: Path, max_age_seconds: float) -> tuple[bool, str]:
    try:
        status = read_status(path)
        updated_at = datetime.fromisoformat(str(status["updated_at"]))
        age = (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds()
    except (ConfigurationError, KeyError, TypeError, ValueError) as error:
        return False, str(error)
    state = status.get("state")
    if state not in {"running", "sending", "paused"}:
        return False, f"scheduler state is {state!r}"
    if age > max_age_seconds:
        return False, f"scheduler heartbeat is {age:.0f}s old"
    return True, f"scheduler is {state}; heartbeat age={age:.0f}s"


def initialize_monitoring():
    """Connect optional shared monitoring when a database URL is configured."""
    database_url = os.getenv("MONITOR_DATABASE_URL") or os.getenv("DATABASE_URL")
    enabled = database_url or _boolean(
        os.getenv("MONITORING_ENABLED", "false"), "MONITORING_ENABLED"
    )
    if not enabled:
        return None

    required = _boolean(
        os.getenv("MONITORING_REQUIRED", "false"), "MONITORING_REQUIRED"
    )
    try:
        from monitoring import (
            MonitoringStore,
            attach_database_logging,
            database_url_from_environment,
        )

        store = MonitoringStore(database_url or database_url_from_environment())
        store.initialize()
        recovered = store.recover_running_commands()
        attach_database_logging(LOGGER, store)
        LOGGER.info("Shared monitoring connected backend=%s", store.backend_name)
        if recovered:
            LOGGER.warning("Marked %s orphaned admin commands as failed", recovered)
        return store
    except Exception as error:
        if required:
            raise ConfigurationError(
                f"Required monitoring database is unavailable: {error}"
            ) from error
        LOGGER.warning("Shared monitoring disabled: %s", error)
        return None


def _record_delivery(
    monitor,
    settings: Settings,
    *,
    source: str,
    started_at: str,
    delivered: bool,
    error: str | None = None,
) -> None:
    if monitor is None:
        return
    try:
        monitor.record_delivery(
            source=source,
            started_at=started_at,
            status="success" if delivered else "failed",
            to_count=len(settings.to_emails),
            cc_count=len(settings.cc_emails),
            attachment=str(settings.attachment_path),
            error=error if not delivered else None,
        )
    except Exception as monitor_error:
        LOGGER.warning("Unable to record delivery monitoring: %s", monitor_error)


def deliver_and_record(settings: Settings, monitor=None, source: str = "manual") -> bool:
    started_at = datetime.now(timezone.utc).isoformat()
    delivered = send_daily_report(settings)
    _record_delivery(
        monitor,
        settings,
        source=source,
        started_at=started_at,
        delivered=delivered,
        error=None if delivered else "delivery failed; see logs",
    )
    return delivered


def process_admin_commands(
    settings: Settings, monitor, paused: bool
) -> tuple[bool, bool]:
    """Execute queued dashboard controls and return pause/change state."""
    if monitor is None:
        return paused, False
    try:
        commands = monitor.claim_commands()
    except Exception as error:
        LOGGER.warning("Unable to poll admin commands: %s", error)
        return paused, False

    changed = False
    for command in commands:
        action = command["action"]
        command_id = int(command["id"])
        succeeded = False
        result = ""
        try:
            LOGGER.info(
                "Admin command started id=%s action=%s requested_by=%s",
                command_id,
                action,
                command.get("requested_by"),
            )
            if action == "pause":
                paused = True
                succeeded = True
                result = "Scheduler paused"
            elif action == "resume":
                paused = False
                succeeded = True
                result = "Scheduler resumed"
            elif action == "send_now":
                succeeded = deliver_and_record(
                    settings, monitor, source="dashboard"
                )
                result = "Email delivered" if succeeded else "Delivery failed"
            elif action == "check_smtp":
                succeeded = verify_smtp_connection(settings)
                result = (
                    "SMTP authentication succeeded"
                    if succeeded
                    else "SMTP authentication failed"
                )
            elif action == "check_config":
                size = validate_runtime(settings)
                succeeded = True
                result = f"Configuration valid; attachment bytes={size}"
            elif action == "dry_run":
                message = build_message(
                    from_email=settings.from_email,
                    to_emails=settings.to_emails,
                    cc_emails=settings.cc_emails,
                    subject=settings.subject,
                    body=settings.body,
                    attachment_path=settings.attachment_path,
                    max_attachment_bytes=settings.max_attachment_bytes,
                )
                succeeded = True
                result = f"Message built successfully; bytes={len(message.as_bytes())}"
            else:
                result = f"Unsupported command: {action}"
        except Exception as error:
            result = str(error)
            LOGGER.exception(
                "Admin command failed id=%s action=%s: %s",
                command_id,
                action,
                error,
            )
        try:
            monitor.finish_command(command_id, succeeded, result)
        except Exception as error:
            LOGGER.warning("Unable to finish admin command id=%s: %s", command_id, error)
        LOGGER.info(
            "Admin command finished id=%s action=%s success=%s result=%s",
            command_id,
            action,
            succeeded,
            result,
        )
        changed = True
    return paused, changed


def run_scheduler(
    settings: Settings, stop_event: Event | None = None, monitor=None
) -> None:
    """Run one timezone-aware daily job until a shutdown signal is received."""
    event = stop_event or Event()
    is_local = settings.timezone_name.casefold() == "local"
    tz = None if is_local else timezone_from_name(settings.timezone_name)
    next_run = next_daily_run(settings.schedule_time, timezone_name=settings.timezone_name)
    last_attempt: str | None = None
    last_success: str | None = None
    last_error: str | None = None
    next_heartbeat = 0.0
    started_at = datetime.now(timezone.utc).isoformat()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    paused = False

    if monitor is not None:
        try:
            previous_state = monitor.get_state()
            paused = bool(previous_state and previous_state.get("paused"))
        except Exception as error:
            LOGGER.warning("Unable to restore monitoring state: %s", error)

    def persist_state(state: str) -> None:
        values = {
            "state": state,
            "paused": paused,
            "pid": os.getpid(),
            "worker_id": worker_id,
            "schedule_time": settings.schedule_time,
            "timezone_name": settings.timezone_name,
            "next_run": next_run.isoformat(),
            "last_attempt": last_attempt,
            "last_success": last_success,
            "last_error": last_error,
            "started_at": started_at,
            "config_json": json.dumps(
                {
                    "attachment": str(settings.attachment_path),
                    "cc_count": len(settings.cc_emails),
                    "recipient_count": len(settings.to_emails),
                    "smtp_host": settings.smtp_host,
                    "smtp_port": settings.smtp_port,
                    "smtp_security": settings.smtp_security,
                }
            ),
        }
        write_status(
            settings.status_file,
            state=state,
            paused=paused,
            next_run=next_run.isoformat(),
            last_attempt=last_attempt,
            last_success=last_success,
            last_error=last_error,
        )
        if monitor is not None:
            try:
                monitor.upsert_state(values)
            except Exception as error:
                LOGGER.warning("Unable to persist shared monitoring state: %s", error)

    LOGGER.info(
        "Scheduler started time=%s timezone=%s next_run=%s",
        settings.schedule_time,
        settings.timezone_name,
        next_run.isoformat(),
    )
    if paused:
        LOGGER.warning("Scheduler restored in paused state")

    try:
        while not event.is_set():
            paused, command_changed = process_admin_commands(
                settings, monitor, paused
            )
            if command_changed:
                next_heartbeat = 0.0

            now = datetime.now() if is_local else datetime.now(tz)
            if paused and now >= next_run:
                LOGGER.info("Scheduled delivery skipped while scheduler is paused")
                next_run = next_daily_run(
                    settings.schedule_time, timezone_name=settings.timezone_name
                )
                next_heartbeat = 0.0
            elif now >= next_run:
                last_attempt = datetime.now(timezone.utc).isoformat()
                persist_state("sending")
                try:
                    delivered = deliver_and_record(
                        settings, monitor, source="schedule"
                    )
                except Exception as error:  # Keep the long-running worker alive.
                    LOGGER.exception("Unexpected report job failure: %s", error)
                    delivered = False
                    last_error = str(error)
                if delivered:
                    last_success = datetime.now(timezone.utc).isoformat()
                    last_error = None
                elif last_error is None:
                    last_error = "delivery failed; see logs"
                next_run = next_daily_run(
                    settings.schedule_time, timezone_name=settings.timezone_name
                )
                next_heartbeat = 0.0
                LOGGER.info("Next report scheduled for %s", next_run.isoformat())

            monotonic_now = time.monotonic()
            if monotonic_now >= next_heartbeat:
                persist_state("paused" if paused else "running")
                next_heartbeat = monotonic_now + settings.heartbeat_seconds

            current_time = datetime.now() if is_local else datetime.now(tz)
            seconds_remaining = max((next_run - current_time).total_seconds(), 0)
            event.wait(min(settings.poll_seconds, max(seconds_remaining, 0.1)))
    finally:
        persist_state("stopped")
        LOGGER.info("Scheduler stopped")


def install_signal_handlers(stop_event: Event) -> None:
    """Convert container/service termination signals into graceful shutdown."""
    def request_shutdown(signum, _frame):
        LOGGER.info("Shutdown requested signal=%s", signum)
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            signal.signal(signum, request_shutdown)


def validate_runtime(settings: Settings) -> int:
    """Perform side-effect-free startup validation and return attachment size."""
    return validate_attachment(settings.attachment_path, settings.max_attachment_bytes)


def safe_settings_summary(settings: Settings, attachment_size: int) -> dict[str, Any]:
    """Return operational settings without credentials."""
    return {
        "attachment": str(settings.attachment_path),
        "attachment_bytes": attachment_size,
        "cc_count": len(settings.cc_emails),
        "log_file": str(settings.log_file) if settings.log_file else None,
        "recipients_count": len(settings.to_emails),
        "schedule_time": settings.schedule_time,
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_security": settings.smtp_security,
        "status_file": str(settings.status_file),
        "timezone": settings.timezone_name,
    }


def _status_path_from_environment() -> Path:
    return _project_path(
        os.getenv("EMAIL_STATUS_FILE", "email_scheduler_status.json")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--send-now", action="store_true", help="send one report now")
    actions.add_argument(
        "--dry-run",
        action="store_true",
        help="build and validate the message without contacting SMTP",
    )
    actions.add_argument(
        "--check-config", action="store_true", help="validate configuration and files"
    )
    actions.add_argument(
        "--check-smtp",
        action="store_true",
        help="authenticate to SMTP without sending a message",
    )
    actions.add_argument("--status", action="store_true", help="print worker status JSON")
    actions.add_argument(
        "--healthcheck", action="store_true", help="check that the worker heartbeat is fresh"
    )
    args = parser.parse_args()

    if args.status:
        try:
            print(json.dumps(read_status(_status_path_from_environment()), indent=2))
            return 0
        except ConfigurationError as error:
            print(error, file=sys.stderr)
            return 3

    if args.healthcheck:
        try:
            max_age = float(os.getenv("EMAIL_HEALTH_MAX_AGE_SECONDS", "300"))
        except ValueError:
            print("EMAIL_HEALTH_MAX_AGE_SECONDS must be a number", file=sys.stderr)
            return 2
        if max_age <= 0:
            print("EMAIL_HEALTH_MAX_AGE_SECONDS must be greater than zero", file=sys.stderr)
            return 2
        healthy, message = status_is_healthy(_status_path_from_environment(), max_age)
        print(message)
        return 0 if healthy else 1

    try:
        settings = load_settings()
        configure_logging(settings=settings)
        if settings.smtp_security == "none":
            LOGGER.warning("SMTP transport encryption is disabled")
        monitor = initialize_monitoring()
        if args.check_smtp:
            return 0 if verify_smtp_connection(settings) else 1

        attachment_size = validate_runtime(settings)

        if args.check_config:
            LOGGER.info("Configuration validation succeeded")
            print(json.dumps(safe_settings_summary(settings, attachment_size), indent=2))
            return 0
        if args.dry_run:
            message = build_message(
                from_email=settings.from_email,
                to_emails=settings.to_emails,
                cc_emails=settings.cc_emails,
                subject=settings.subject,
                body=settings.body,
                attachment_path=settings.attachment_path,
                max_attachment_bytes=settings.max_attachment_bytes,
            )
            print(
                json.dumps(
                    {
                        **safe_settings_summary(settings, attachment_size),
                        "message_bytes": len(message.as_bytes()),
                        "message_id": message["Message-ID"],
                    },
                    indent=2,
                )
            )
            return 0
        if args.send_now:
            return 0 if deliver_and_record(settings, monitor, "command-line") else 1

        stop_event = Event()
        install_signal_handlers(stop_event)
        run_scheduler(settings, stop_event, monitor)
        return 0
    except (ConfigurationError, AttachmentError) as error:
        configure_logging()
        LOGGER.error("Startup validation failed: %s", error)
        return 2
    except KeyboardInterrupt:
        LOGGER.info("Interrupted")
        return 0
    except Exception as error:
        configure_logging()
        LOGGER.exception("Fatal worker error: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
