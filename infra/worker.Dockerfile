FROM python:3.11-slim
WORKDIR /app
COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && \
    useradd --create-home --uid 10001 docqa && mkdir -p /data/documents && chown -R docqa:docqa /data /app
COPY worker/app /app/app
USER docqa
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
