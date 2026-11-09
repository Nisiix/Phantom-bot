FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TERM=xterm-256color
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m desk && mkdir -p /app/data && chown -R desk /app
USER desk
CMD ["python", "run.py"]
