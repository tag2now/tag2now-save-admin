# tdt_admin_server.py and the scheduled floors it runs. The CLI keeps running
# on the RPCN host itself.
FROM python:3.12-alpine

WORKDIR /app
# .dockerignore is the one list of what ships
COPY script/ ./

# Every path tdt_admin writes to (saves, backups, audit log) is mounted at the
# same path as on the host, so the host CLI can read back what this wrote:
# `floor --redo` reopens the backup paths recorded in the audit log.
# USER names the scheduled floors in the audit log; web writes name their admin.
ENV PYTHONUNBUFFERED=1 \
    TDT_ADMIN_BIND=0.0.0.0:8000 \
    USER=save-admin
EXPOSE 8000

CMD ["python", "tdt_admin_server.py"]
