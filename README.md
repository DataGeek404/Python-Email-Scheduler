
# Python Email Scheduler with Attachment

## Overview:
This project demonstrates how to automate sending daily email reports with attachments using Python. It uses key Python libraries like `smtplib` for sending emails and `schedule` for automating the process. The project is designed to be deployed locally or on cloud platforms such as **Heroku**.

## Features:
- Sends daily reports via email, including an attachment (e.g., PDF file).
- Supports CC for additional email recipients.
- Uses environment variables for sensitive email credentials.
- Logs email-sending success or errors into a log file.
- Automates the process with a daily scheduler.

## Step-by-Step Guide:

### 1. Clone the Project:
   First, clone the project from GitHub to your local machine:
   
   ```bash
   git clone https://github.com/DataGeek404/Python-Email-Scheduler.git
   cd Python-Email-Scheduler
   ```

### 2. Install Dependencies:
   Install the required Python libraries using `pip`:
   
   ```bash
   pip install -r requirements.txt
   ```

   If there’s no `requirements.txt` file in the project, manually install the necessary libraries:
   
   ```bash
   pip install schedule
   ```

### 3. Set Up Environment Variables:
   You need to set up environment variables for your email credentials so they are not hardcoded in the project. The variables should store your email address and password or app-specific password.

#### Locally:
   For **Windows**, run these commands in your terminal to set environment variables:
   
   ```bash
   set EMAIL_HOST_USER=your-email@gmail.com
   set EMAIL_HOST_PASSWORD=your-app-password
   ```

   For **Linux/macOS**, run:
   
   ```bash
   export EMAIL_HOST_USER=your-email@gmail.com
   export EMAIL_HOST_PASSWORD=your-app-password
   ```

#### On Heroku:
   To set up environment variables in **Heroku**, follow these steps:
   - Log in to your [Heroku dashboard](https://dashboard.heroku.com).
   - Go to **Settings** > **Config Vars**.
   - Add the following key-value pairs:
     - `EMAIL_HOST_USER` = `your-email@gmail.com`
     - `EMAIL_HOST_PASSWORD` = `your-app-password`

### 4. Place Attachment in the Reports Directory:
   Ensure that the file you want to send as an attachment (e.g., `jay.pdf`) is placed in the `Reports/` directory inside the project folder. The script will attach this file to the emails.

   ```plaintext
   /Python-Email-Scheduler/
       ├── Reports/
       │   └── jay.pdf
       └── daily_email_report.py
   ```

### 5. Execute the Script:

#### Locally:
   Once the environment variables are set and the necessary files are in place, run the script locally:
   
   ```bash
   python daily_email_report.py
   ```

   This will start the daily email scheduler, and the log file (`email_scheduler.log`) will track the status of emails.

#### On Heroku:
   If you want to deploy the project to **Heroku** to automate the email scheduling in the cloud, follow these steps:

   - Log in to Heroku:
     ```bash
     heroku login
     ```

   - Create a Heroku app:
     ```bash
     heroku create python-email-scheduler
     ```

   - Push the code to Heroku:
     ```bash
     git add .
     git commit -m "Deploy email scheduler to Heroku"
     git push heroku master
     ```

### 6. Configure Scheduler Timing:
   By default, the script is set to send emails daily at a specific time. You can modify the scheduling time in the `daily_email_report.py` script, for example:
   
   ```python
   schedule.every().day.at("22:49").do(send_daily_report)
   ```

   Change `"22:49"` to any valid time in **24-hour format**.

### 7. Monitor Logs and Email Status:

#### Locally:
   - The script logs each email sent (or any errors) to the `email_scheduler.log` file in the root directory. Check this file to monitor the status of the emails.

#### On Heroku:
   - Use the following command to monitor Heroku logs:
   
     ```bash
     heroku logs --tail
     ```

### 8. Troubleshooting:
   Here are some common issues and fixes:

   1. **'NoneType' object has no attribute 'encode'**:
      - This error means the environment variables are not set correctly. Ensure that the `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD` are properly set as environment variables either locally or in Heroku.

   2. **Invalid Credentials on Heroku**:
      - If you encounter this error during deployment, ensure that the `HEROKU_API_KEY` is set correctly in GitHub secrets (if using GitHub Actions for deployment) or set directly in the Heroku Config Vars.

   3. **File Not Found Error**:
      - Ensure that the file you are attaching (e.g., `jay.pdf`) is in the correct directory (`Reports/`) and the path is set correctly in the script.

### Conclusion:
This Python project automates daily email reporting with attachments and can be customized for various use cases, such as sending reports, backups, or notifications. It can run locally or on cloud platforms like Heroku, making it a versatile tool for automated email scheduling.

Feel free to customize the project for your needs and deploy it to Heroku or any other cloud platform for continuous use!
