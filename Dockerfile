FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOBOPS_CONTROL_HOME=/var/lib/jobops/control

WORKDIR /app

RUN groupadd --gid 10001 jobops \
    && useradd --uid 10001 --gid jobops --home-dir /nonexistent --no-create-home jobops

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

USER 10001:10001
EXPOSE 9000

CMD ["python", "-m", "jobops.control_plane", "serve", "--host", "0.0.0.0", "--port", "9000"]
