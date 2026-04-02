FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi uvicorn

# Copy application code
COPY . .

# Ensure data/logs dirs exist
RUN mkdir -p logs data

EXPOSE 8888

CMD ["python", "dashboard.py", "--port", "8888"]
