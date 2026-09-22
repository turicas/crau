# Development and test image: Python 3.14 on Debian trixie with Chromium, Firefox and Lightpanda.
# The browsers are installed by `crau browser install`, so building this image is also its test on Debian.
FROM python:3.14-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    REQUIRE_BROWSERS=1

# procps: `ps`, used by the signal-handling tests; openssl: self-signed certificate for the TLS test;
# build-essential and the libxml2/libxslt headers: lxml has no wheel for every Python version
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash build-essential ca-certificates git libxml2-dev libxslt1-dev \
       make openssl procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-development.txt /app/
RUN pip install --no-cache-dir -r requirements-development.txt

COPY . /app
RUN pip install --no-cache-dir --no-deps -e . \
    && crau browser install all --verbose \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user to run the tools (same UID/GID as the host user, so files in the bind mount stay yours)
ARG UID=1000
ARG GID=1000
RUN groupadd --gid "${GID}" dev \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /bin/bash dev \
    && chown -R dev:dev /app
USER dev

CMD ["sleep", "infinity"]
