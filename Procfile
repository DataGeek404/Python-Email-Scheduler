release: python -u daily_email_report.py --check-config
web: gunicorn "admin_dashboard:create_app()" --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
worker: python -u daily_email_report.py
