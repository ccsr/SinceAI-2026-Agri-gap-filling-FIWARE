# Container for the layered gap filling of tenant apetit_ccn_post_ml (gapfill/gapfill_ccn.py).
FROM python:3.12-slim
WORKDIR /work
COPY requirements-gapfill.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-gapfill.txt
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "gapfill/gapfill_ccn.py"]
CMD ["--help"]
