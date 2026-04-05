FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi uvicorn

COPY . .

RUN mkdir -p data logs

EXPOSE 3000

CMD ["python", "dashboard.py", "--port", "3000"]
