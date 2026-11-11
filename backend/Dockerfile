FROM python:3.12-slim AS package

WORKDIR /build
COPY backend/pyproject.toml backend/README.md ./backend/
COPY backend/src/ ./backend/src/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels "./backend[modal]"

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MATCHPULSE_PROJECT_ROOT=/app

WORKDIR /app

COPY --from=package /wheels/ /wheels/
RUN pip install --no-cache-dir --no-index --find-links=/wheels "touchline-backend[modal]"

# Install the same application into the isolated caption interpreter without
# pulling the backend's incompatible websockets dependency into that environment.
COPY backend/requirements-captions.txt /tmp/requirements-captions.txt
RUN python -m venv /opt/matchpulse-captions \
    && /opt/matchpulse-captions/bin/pip install --no-cache-dir -r /tmp/requirements-captions.txt \
    && /opt/matchpulse-captions/bin/pip install --no-deps --no-index --find-links=/wheels touchline-backend \
    && rm -rf /wheels
ENV MATCHPULSE_CAPTION_PYTHON=/opt/matchpulse-captions/bin/python

EXPOSE 8000 8031

CMD ["python", "-m", "touchline.main"]
