FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MMFDB_CONFIG=/etc/mmfdb/mmfdb.yaml

RUN addgroup --system mmfdb && adduser --system --ingroup mmfdb mmfdb

WORKDIR /opt/mmfdb
COPY pyproject.toml setup.py README.md LICENSE docker-constraints.txt ./
COPY src ./src
RUN python -m pip install --no-cache-dir -c docker-constraints.txt '.[postgres,ldap,s3]'

COPY mmfdb.example.yaml /etc/mmfdb/mmfdb.yaml
RUN mkdir -p /var/lib/mmfdb /etc/mmfdb && chown -R mmfdb:mmfdb /var/lib/mmfdb

USER mmfdb
VOLUME ["/var/lib/mmfdb"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]

CMD ["mmfdb", "serve", "--config", "/etc/mmfdb/mmfdb.yaml"]
