FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt
COPY packages ./packages
COPY server ./server
COPY profiles ./profiles
COPY tools ./tools
ENV PYTHONUNBUFFERED=1 TBOX_DATA_DIR=/data TBOX_HOST=0.0.0.0 TBOX_PORT=8080 TBOX_TRUST_PROXY=1 TBOX_SEED_DEMO=0 TBOX_SIMULATOR=0
VOLUME /data
EXPOSE 8080
RUN useradd -r -u 10001 tbox && mkdir -p /data && chown tbox /data
USER tbox
CMD ["python", "server/run_server.py"]
