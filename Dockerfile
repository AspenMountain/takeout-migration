FROM python:3.12-slim

RUN groupadd --system app && useradd --system --gid app app

WORKDIR /app

# Install dependencies in a separate layer so rebuilds after code changes are fast.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Uploads are processed entirely in /tmp (one tempdir per request, deleted on completion).
# Raise the default gunicorn worker timeout to give large ZIPs room to process.
ENV TMPDIR=/tmp \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')"

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--timeout", "120", \
     "--forwarded-allow-ips", "*", \
     "app:app"]
