FROM python:3.11-alpine

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-compile -r /app/requirements.txt

COPY app.py /app/app.py
COPY openvre_scheduler /app/openvre_scheduler
# Built-in workload templates. Override with OPENVRE_TEMPLATE_DIR (e.g. a mounted ConfigMap).
COPY templates /app/templates

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

USER 65534:65534

CMD ["python", "/app/app.py"]
