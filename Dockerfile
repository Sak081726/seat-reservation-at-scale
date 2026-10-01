FROM python:3.12-slim
WORKDIR /app
COPY app.py burst.py ./
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1 DATABASE_PATH=/data/reservations.db PORT=8000
EXPOSE 8000
CMD ["python", "app.py"]
