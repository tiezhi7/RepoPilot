# Target: Debian 12 / Linux AMD64 / CPython 3.12.16.
# Build with --platform linux/amd64 and backend as the build context.
FROM python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN python -m venv /opt/venv

COPY locks/linux-py312-runtime.txt /tmp/linux-py312-runtime.txt
RUN /opt/venv/bin/python -m pip --isolated --timeout 60 --retries 2 install \
        --index-url https://pypi.org/simple --only-binary=:all: --no-cache-dir pip==26.1.2 \
    && /opt/venv/bin/python -m pip --isolated --timeout 60 --retries 2 install \
        --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
        --no-cache-dir -r /tmp/linux-py312-runtime.txt \
    && /opt/venv/bin/python -m pip check

# Keep application files readable by the fixed non-root runtime identity.
COPY --chmod=0555 app/ /app/app/
USER 10001:10001

EXPOSE 8000
CMD ["/opt/venv/bin/python", "-B", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
