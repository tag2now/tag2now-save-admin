# tdt_admin_server.py only. The CLI keeps running on the RPCN host itself.
FROM python:3.12-alpine

WORKDIR /app
COPY script/tdt_admin.py script/tdt_admin_server.py ./

# Every path tdt_admin writes to (saves, backups, audit log) is mounted at the
# same path as on the host, so the host CLI can read back what this wrote:
# `floor --redo` reopens the backup paths recorded in the audit log.
ENV PYTHONUNBUFFERED=1 \
    TDT_ADMIN_BIND=0.0.0.0:8000
EXPOSE 8000

CMD ["python", "tdt_admin_server.py"]
