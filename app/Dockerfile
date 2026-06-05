FROM python:3.12-slim

# socat: creates the in-container /dev/ttyNILAN PTY from the ESP raw-TCP stream.
RUN apt-get update && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Vendored frodef protocol library + our wrapper
COPY vendor/nilan_cts600.py /app/nilan_cts600.py
COPY app/nilan_api.py /app/nilan_api.py
COPY app/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8642
ENTRYPOINT ["/app/entrypoint.sh"]
