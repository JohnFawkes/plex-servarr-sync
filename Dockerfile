# Use a lightweight Python base image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy requirements and install them
# We install flask, requests, and python-dotenv
RUN pip install --no-cache-dir flask requests python-dotenv plexapi

# Copy the script into the container
COPY plex_servarr_webhook.py .

# Expose the port defined in the script (default 5000)
EXPOSE 5000

ENV PYTHONUNBUFFERED=1

# Run the script
CMD ["python", "plex_servarr_webhook.py"]
