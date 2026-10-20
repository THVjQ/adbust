# adbust web. The browser bundle (static/app.js) and the phone helper
# (static/adbust-applist.dex) are committed, so the image needs only Python.
FROM python:3.13-alpine
WORKDIR /app
COPY server.py ./
COPY static ./static
ENV ADBUST_DATA=/data ADBUST_PORT=8017 PYTHONUNBUFFERED=1
EXPOSE 8017
# 568 is TrueNAS's "apps" user, which owns the data folder.
USER 568:568
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8017/api/health || exit 1
# Python as PID 1 ignores SIGTERM; SIGINT stops serve_forever cleanly.
STOPSIGNAL SIGINT
CMD ["python3", "server.py"]
