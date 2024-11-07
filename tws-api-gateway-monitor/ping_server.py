from ib_api import REST
import time
from datetime import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from twilio.rest import Client
import logging

# Load environment variables from .env file
load_dotenv()
logging.basicConfig(level=logging.INFO)

# Retrieve server URLs from .env and split into a list
server_urls = os.getenv("SERVER_URLS", "").split(",")

# Email settings from .env
smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
smtp_port = int(os.getenv("SMTP_PORT", 587))
smtp_username = os.getenv("SMTP_USERNAME")
smtp_password = os.getenv("SMTP_PASSWORD")
sender_email = os.getenv("SENDER_EMAIL")
receiver_email = os.getenv("RECEIVER_EMAIL")

# Twilio settings from .env
twilio_enabled = os.getenv("TWILIO_ENABLED", "false").lower() == "true"
twilio_sid = os.getenv("TWILIO_SID")
twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER")
to_phone_number = os.getenv("TO_PHONE_NUMBER")

# Initialize Twilio client if Twilio is enabled
if twilio_enabled:
    twilio_client = Client(twilio_sid, twilio_auth_token)

def log_status(server_url, message):
    now = datetime.now()
    logging.info(f"{now.strftime('%Y/%m/%d %H:%M:%S')} - {server_url} - {message}")

def send_error_email(server_url, error_message):
    # Create the email content
    subject = f"Error in Server {server_url}"
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = subject
    body = f"An error occurred while checking the server {server_url}:\n\n{error_message}"
    msg.attach(MIMEText(body, 'plain'))

    # Send the email via Gmail SMTP server
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()  # Secure the connection
            server.login(smtp_username, smtp_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        log_status(server_url, "Error email sent successfully.")
    except Exception as e:
        log_status(server_url, f"Failed to send email: {e}")

def make_voice_call(error_message, server):
    if twilio_enabled:
        # Make a voice call using Twilio
        try:
            call = twilio_client.calls.create(
                to=to_phone_number,
                from_=twilio_phone_number,
                twiml=f'<Response><Say>Hello Ilias! TWS api gateway is down. Server: {server} {error_message}</Say></Response>'
            )
            log_status("Twilio", f"Voice call initiated successfully: {call.sid}")
        except Exception as e:
            log_status("Twilio", f"Failed to make voice call: {e}")

def ping_and_check_auth(ib, server_url):
    try:
        status = ib.ping_server()
        log_status(server_url, f"Ping status: {status}")
        if not status.get("iserver", {}).get("authStatus", {}).get("authenticated", True):
            ib.re_authenticate()
            time.sleep(5)
            status = ib.get_auth_status()
            log_status(server_url, f"Reauthentication status: {status}")
    except Exception as e:
        error_message = f"Error during ping or reauthentication: {e}"
        log_status(server_url, error_message)
        send_error_email(server_url, error_message)
        make_voice_call(error_message, server)  # Make a voice call on error, if Twilio is enabled

def main():
    sleep_interval = 60 * 5
    logging.info("App started....")
    while True:
        for server_url in server_urls:
            if server_url.strip():  # Make sure server_url is not empty
                ib = REST(url=server_url.strip(), ssl=False)
                ping_and_check_auth(ib, server_url.strip())
        time.sleep(sleep_interval)

if __name__ == "__main__":
    main()
