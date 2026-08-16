FROM python:3.12-slim
RUN pip install --no-cache-dir segno
WORKDIR /app
COPY marrow_server.py .
VOLUME /data
EXPOSE 8800
CMD ["python3", "marrow_server.py", "--data", "/data"]
