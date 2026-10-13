FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default values — override via docker-compose environment or -e flags
ENV PORT=5000
ENV HOST_ADDR=127.0.0.1
ENV SEED_PEER=""
ENV DIFFICULTY=3
ENV MINING_REWARD=10.0
ENV MINER_ADDRESS=miner_default

EXPOSE ${PORT}

CMD ["sh", "-c", "python3 main.py ${PORT} ${SEED_PEER}"]
