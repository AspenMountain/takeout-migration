import multiprocessing
import os

# Scale to available CPUs; override with WEB_CONCURRENCY env var.
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))
worker_class = "sync"

bind = "0.0.0.0:8000"

# Large ZIPs can take a while to process; give workers room to breathe.
timeout = 120

# Recycle workers after N requests to avoid slow memory growth.
max_requests = 500
max_requests_jitter = 50

# Emit access logs to stdout (Docker-friendly).
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Load app code once in the master process, then fork — saves ~50 MB per worker
# via copy-on-write.  Safe here because the app opens no connections at import.
preload_app = True

# Trust X-Forwarded-For from any upstream proxy (e.g. nginx, Cloud Run, etc.)
forwarded_allow_ips = "*"
