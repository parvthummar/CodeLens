# Matches the documented dev environment (backend/cr_venv is 3.11).
FROM python:3.11-slim

# git is a runtime dependency, not a build one: github_service shells out to
# `git clone` for every indexing run.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No venv inside the image — the container is already the isolation boundary,
# and a venv only adds a PATH to get wrong.
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt

COPY . .

# Non-root. Created after the copy so the app tree stays root-owned, and the
# process cannot rewrite its own code. Clones go to tempfile.mkdtemp() under
# /tmp, which is world-writable, so nothing else needs chown.
RUN useradd --create-home --uid 10001 appuser \
    && chmod +x /app/docker-entrypoint.sh
USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
