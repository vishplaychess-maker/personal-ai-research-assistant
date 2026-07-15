FROM python:3.12-slim

WORKDIR /app

# Create persistent data directories with write permissions
RUN mkdir -p /data /data/uploads

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
