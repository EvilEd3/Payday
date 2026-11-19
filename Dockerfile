FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd --gid 10001 payday && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /srv payday
COPY app ./app
ENV DATABASE_URL=sqlite:////data/budget.db \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
# Starts as root only long enough to hand /data to the "payday" user, then drops to it (see app/start.py).
CMD ["python", "-m", "app.start"]
