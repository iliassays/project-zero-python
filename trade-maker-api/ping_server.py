from ib_api import REST
import time
from datetime import datetime

def log_status(server_url, message):
    now = datetime.now()
    print(f"{now.strftime('%Y/%m/%d %H:%M:%S')} - {server_url} - {message}")

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
        log_status(server_url, f"Error during ping or reauthentication: {e}")

def main():
    sleep_interval = 60 * 5
    server_urls = [
        "https://3.79.183.15:5000",
    ]

    while True:
        for server_url in server_urls:
            ib = REST(url=server_url, ssl=False)
            ping_and_check_auth(ib, server_url)
        time.sleep(sleep_interval)

if __name__ == "__main__":
    main()
