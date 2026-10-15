# Render sandbox for UNTRUSTED, LLM-written Manim code.
#
# Build:   docker build -t stv-manim-sandbox:latest sandbox/
# The official image ships Manim + cairo/pango + ffmpeg + a TeX install with
# dvisvgm, so MathTex works. On Apple Silicon, if this tag has no arm64 variant
# (`docker manifest inspect manimcommunity/manim:v0.19.0`), build with
# `--platform linux/amd64` and Docker Desktop will run it under Rosetta.
#
# The image alone is not the sandbox: the runtime flags in sandbox/run.py are
# (--network none, --read-only, --memory/--cpus/--pids-limit, --cap-drop ALL,
# no-new-privileges, tmpfs /tmp, host-side kill on timeout).
FROM manimcommunity/manim:v0.19.0

USER root
# Dedicated unprivileged user with no login shell and no writable home.
RUN useradd --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sandbox \
    && mkdir -p /opt/stv /work /out \
    && chown sandbox:sandbox /out
COPY render_entry.py /opt/stv/render_entry.py
RUN chmod 0444 /opt/stv/render_entry.py

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /work
USER 10001:10001

# The host runner passes the full command; default is a harmless self-check.
ENTRYPOINT []
CMD ["python", "-c", "import manim; print('manim', manim.__version__)"]
