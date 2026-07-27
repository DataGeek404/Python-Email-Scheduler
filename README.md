# Python Email Scheduler

Send a report attachment by email once per day. SMTP credentials, recipients,
the attachment, and the schedule are configured with environment variables;
no credentials are stored in the source code.

## Features

- Sends a daily email with a PDF or any other file attachment.
- Supports multiple primary and CC recipients.
- Reads SMTP credentials and message settings from environment variables.
- Uses STARTTLS by default.
- Records successful deliveries and errors in `email_scheduler.log`.
- Provides both a persistent daily worker and a one-time send command.

## Requirements

- Python 3.10 or newer
- An SMTP account. For Gmail, use an app password rather than your normal
  account password.

Install the project requirements (currently standard-library only):

```powershell
python -m pip install -r requirements.txt
```

## Configuration

Set these variables in the process that will run the scheduler. The
[`.env.example`](.env.example) file is a reference; the application does not
load `.env` files automatically.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `EMAIL_HOST_USER` | Yes | - | SMTP login username |
| `EMAIL_HOST_PASSWORD` | Yes | - | SMTP password or app password |
| `EMAIL_TO` | Yes | - | Comma-separated primary recipients |
| `EMAIL_CC` | No | Empty | Comma-separated CC recipients |
| `EMAIL_FROM` | No | `EMAIL_HOST_USER` | Address shown in the From header |
| `EMAIL_HOST` | No | `smtp.gmail.com` | SMTP hostname |
| `EMAIL_PORT` | No | `587` | SMTP port |
| `EMAIL_USE_TLS` | No | `true` | Enable STARTTLS |
| `EMAIL_TIMEOUT` | No | `30` | SMTP timeout in seconds |
| `REPORT_ATTACHMENT` | No | `Reports/jay.pdf` | File to attach |
| `REPORT_TIME` | No | `23:10` | Daily send time in 24-hour `HH:MM` format |
| `EMAIL_SUBJECT` | No | `Daily Report with Attachment` | Message subject |
| `EMAIL_BODY` | No | Built-in message | Plain-text message body |
| `EMAIL_LOG_FILE` | No | `email_scheduler.log` | Log file path |

Example for PowerShell:

```powershell
$env:EMAIL_HOST_USER = "sender@gmail.com"
$env:EMAIL_HOST_PASSWORD = "your-app-password"
$env:EMAIL_TO = "recipient@example.com"
$env:EMAIL_CC = "manager@example.com,team@example.com"
$env:REPORT_ATTACHMENT = "Reports/jay.pdf"
$env:REPORT_TIME = "08:30"
```

Relative attachment paths are resolved from the directory where the command is
started, so run the commands below from the project root. The schedule uses the
local timezone of the machine or worker process.

## Run

Start the daily scheduler:

```powershell
python daily_email_report.py
```

Send one report immediately, which is useful for checking the configuration:

```powershell
python daily_email_report.py --send-now
```

The command exits with status `0` after a successful one-time send, `1` after a
delivery failure, or `2` for invalid configuration. The included `Procfile`
starts the persistent scheduler as a worker process.

## Logs

Successful sends record the To recipients, CC recipients, and attachment path.
Failures include a traceback for diagnosing missing files, authentication
errors, connection errors, or SMTP rejection. Passwords are never logged.

## Tests

The tests mock SMTP and never send a real message:

```powershell
python -m unittest discover -s tests -v
```

## Security

Keep `.env` and log files out of source control. If a password has ever been
committed to Git, revoke it with the email provider and create a new one;
removing it only from the current source file does not remove it from Git
history.
