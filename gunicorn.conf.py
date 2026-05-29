import multiprocessing
import os

# Scale to available CPUs; override with WEB_CONCURRENCY env var.
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))
# gthread: each worker runs a thread pool.  The worker's main loop keeps
# updating heartbeats while request threads are busy (e.g. streaming a large
# file via sendfile).  sync workers can't do this — a long sendfile call
# blocks the entire worker, the heartbeat stops, and the master kills it.
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", "4"))

bind = "0.0.0.0:8000"

# Worker heartbeat timeout.  With gthread workers the main loop updates
# heartbeats independently of request threads, so long downloads don't
# trigger this.  Keep at 300 s to catch genuinely stuck workers.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))

# Must point to a writable directory — critical on read-only container filesystems
# where gunicorn uses this for worker heartbeat files.
worker_tmp_dir = "/tmp"

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
