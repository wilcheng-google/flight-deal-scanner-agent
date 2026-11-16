FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Run as non-root user for container security hardening
RUN groupadd --gid 10001 scanner && useradd --uid 10001 --gid scanner --create-home scanner

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY flight_scanner_agent ./flight_scanner_agent
COPY deployment ./deployment
COPY evals ./evals

RUN mkdir -p /app/data && chown -R scanner:scanner /app
USER scanner

EXPOSE 8080
CMD ["uvicorn", "deployment.server:fastapi_app", "--host", "0.0.0.0", "--port", "8080"]
