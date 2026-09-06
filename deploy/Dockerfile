FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir websockets==15.0.1
COPY quota_guard/__init__.py quota_guard/signaling.py /app/quota_guard/
COPY server_main.py /app/server_main.py
USER 65534:65534
EXPOSE 48733
CMD ["python", "server_main.py", "--host", "0.0.0.0"]
