#!/bin/bash

# Activate virtual environment (if using one)
source trade_maker_api_app/bin/activate

# Start the application with Uvicorn (corrected)
sudo nohup python3 server.py &  # Added "&" to run in the background

# Keep the script running (optional)  # You can remove this line if desired.
tail -f /dev/null