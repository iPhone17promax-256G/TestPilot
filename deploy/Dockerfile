FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers ANONYMIZED_TELEMETRY=false BROWSER_USE_TELEMETRY=false
WORKDIR /app
COPY requirements.lock /app/
RUN pip install --no-cache-dir -r requirements.lock
RUN playwright install-deps chromium && apt-get update && apt-get install --no-install-recommends -y chromium && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md /app/
COPY src /app/src
RUN pip install --no-deps .
COPY skills /app/skills
COPY web/dist /app/web/dist
COPY examples /app/examples
COPY scripts/setup_https.py /app/scripts/setup_https.py
RUN useradd -u 1000 -m runner
CMD ["python", "-m", "testpilot.serve"]
