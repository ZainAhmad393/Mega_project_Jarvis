FROM python:3.11-slim

WORKDIR /app

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY server.py client.py voice_service.py tools.py memory.py musicLibrary.py favicon.jpg ./
COPY face/ ./face/

RUN mkdir -p /app/temp/cache /app/logs /app/data

EXPOSE 7860

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
