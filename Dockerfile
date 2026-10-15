# App image for the API and the worker (NOT the render sandbox; see sandbox/Dockerfile).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI only: the worker launches sandbox containers on the HOST daemon
# through the mounted /var/run/docker.sock (see docker-compose.yml).
ARG DOCKER_CLI_VERSION=27.3.1
RUN arch="$(uname -m)" \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
