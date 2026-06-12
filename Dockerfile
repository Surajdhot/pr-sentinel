FROM python:3.11-slim

RUN useradd --create-home --shell /usr/sbin/nologin sentinel

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R sentinel:sentinel /app

USER sentinel

EXPOSE 8000

CMD ["uvicorn", "webhook:app", "--host", "0.0.0.0", "--port", "8000"]
