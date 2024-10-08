# Python Email Scheduler with Attachment

## Overview:
This project demonstrates how to automate sending daily email reports with attachments using Python. It uses key Python libraries like `smtplib` for sending emails and `schedule` for automating the process. The project is designed to be deployed locally or on cloud platforms such as **Heroku**.

## Features:
- Sends daily reports via email, including an attachment (e.g., PDF file).
- Supports CC for additional email recipients.
- Uses environment variables for sensitive email credentials.
- Logs email-sending success or errors into a log file.
- Automates the process with a daily scheduler.

## Prerequisites:
1. **Basic Python Setup**: Ensure that you have Python 3.x installed.
2. **Email Account**: A Gmail account (with app-specific passwords if two-factor authentication is enabled).

## Step-by-Step Guide:

### 1. Clone the Project:
```bash
git clone https://github.com/DataGeek404/Python-Email-Scheduler.git
cd Python-Email-Scheduler

pip install -r requirements.txt

## Step-by-Step Guide:

