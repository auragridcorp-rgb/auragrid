#!/bin/bash
echo "Writing Dockerfile.api..."
cat << 'DOCKERAPI' > Dockerfile.api
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "main.py"]
DOCKERAPI

echo "Writing Dockerfile.agents..."
cat << 'DOCKERAGENTS' > Dockerfile.agents
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "auragrid_swarm.py"]
DOCKERAGENTS

echo "Writing Aperture Dockerfile..."
mkdir -p aperture-master
cat << 'DOCKERAPERTURE' > aperture-master/Dockerfile
FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY . .
RUN CGO_ENABLED=0 go build -o aperture .

FROM alpine:latest
WORKDIR /root/
COPY --from=builder /app/aperture /usr/local/bin/aperture
EXPOSE 8080 8081
ENTRYPOINT ["aperture"]
DOCKERAPERTURE

echo "Launching Docker Compose..."
sudo docker compose up --build -d
