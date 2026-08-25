# Deployment image. Kept deliberately boring: one stage, no compiler, no wheels built
# from source -- everything this app needs ships as a pure-Python or manylinux wheel.
FROM python:3.10-slim

# Bytecode is written at build time instead of on every cold start, and stdout is
# unbuffered so logs from a crashed worker actually reach the platform's log view.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Metadata first, then source. A source-only edit reuses the cached dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src

# Installed, not run from the checkout: `package-data` in pyproject ships the templates
# and stylesheet, and installing is the only thing that proves it. Running uvicorn against
# the source tree would hide a missing-template failure until after deploy.
# No [dev] and no [engines]: the test suite and the database drivers are not part of the
# product, and psycopg[binary] alone is ~30MB of image for code that never executes here.
RUN pip install --no-cache-dir .

# Only the fallback. With TURSO_DATABASE_URL and TURSO_AUTH_TOKEN set, schemas live in a
# hosted database and nothing is written here; without them the app keeps a local SQLite
# file, which on a free instance does not survive a restart (D9, D51).
ENV SCHEMAVCS_DATA=/data

# Non-root, and the data directory is chowned before the drop. Doing it after would leave
# the app unable to create the workspace file it needs on the first request.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data
USER app

EXPOSE 8000

# $PORT is what the platform injects; 8000 is the local fallback. Binding 0.0.0.0 rather
# than the default localhost is what makes the container reachable from outside itself.
# `sh -c` so the variable is expanded at run time rather than frozen at build time.
CMD ["sh", "-c", "exec uvicorn schemavcs.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
