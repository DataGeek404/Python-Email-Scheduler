import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import schedule
import time
import os
import logging

# Setup logging
logging.basicConfig(filename='email_scheduler.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)s:%(message)s')

# Function to send the email with an attachment, including CC
def send_email_with_attachment(subject, body, to_email, cc_email, file_path):
    # Email settings
    smtp_server = 'smtp.gmail.com'
    smtp_port = 587
    from_email = 'kibejay61@gmail.com'
    from_password = 'wnpn tihx zgbx etiv'  # Use app password

    # Create the message
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Cc'] = cc_email  # Add CC here
    msg['Subject'] = subject

    # Attach the email body
    msg.attach(MIMEText(body, 'plain'))

    # Check if the file exists before attaching
    if os.path.isfile(file_path):
        try:
            # Attach the file
            with open(file_path, "rb") as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename= {os.path.basename(file_path)}')
                msg.attach(part)
        except Exception as e:
            logging.error(f"Error attaching file: {file_path}, error: {e}")
            print(f"Error attaching file: {e}")
            return
    else:
        logging.error(f"File not found: {file_path}")
        print(f"File not found: {file_path}")
        return

    try:
        # Setup the SMTP server
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()  # Use TLS encryption
        server.login(from_email, from_password)  # Log into the email account

        # Combine recipient and CC emails for sending
        recipients = [to_email] + [cc_email] if cc_email else []

        # Send the email to both To and CC recipients
        server.sendmail(from_email, recipients, msg.as_string())
        logging.info(f'Email with attachment sent to {to_email} and CCed to {cc_email}')
        print(f'Email with attachment sent to {to_email} and CCed to {cc_email}')

        # Close the server connection
        server.quit()
    except Exception as e:
        logging.error(f"Error sending email: {e}")
        print(f"Error sending email: {e}")

# Define the daily report content and schedule
def send_daily_report():
    subject = "Daily Report with Attachment"
    body = "Hello James, find the report below. This is your daily report.\n\n- Summary of tasks\n- Analytics overview\n- Progress report"
    to_email = "techspaceerror404@gmail.com"
    cc_email = "awanzihassan1@gmail.com"  # Add the CC email here
    file_path = "C:\\Users\\lenovo\\PythonAutomation\\Reports\\jay.pdf"  # Replace with the actual path to your file
    send_email_with_attachment(subject, body, to_email, cc_email, file_path)

# Schedule the email to be sent every day at a specific time (e.g., 12:15 PM)
schedule.every().day.at("18:50").do(send_daily_report)

# Run the scheduler in an infinite loop
if __name__ == "__main__":
    logging.info("Daily report email scheduler started.")
    print("Daily report email scheduler started.")
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute
