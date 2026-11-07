# Container for running the real HESTIA engine on the exported cycles.
# Needs outbound internet to api.hestia.earth and www.hestia.earth.
FROM python:3.12-slim
WORKDIR /work
COPY requirements-hestia.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-hestia.txt
ENV ENABLE_DROUGHT_INDEX=false \
    LOG_LEVEL=WARNING \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["python3"]
CMD ["hestia/run_hestia.py", "--help"]
