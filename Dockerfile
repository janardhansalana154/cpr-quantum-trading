# Multi-stage Dockerfile for CPR Quantum
# Stage 1: Build the React frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /build
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: Build the FastAPI backend and serve static frontend
FROM python:3.12-slim
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend python codebase
COPY . .

# Copy static frontend build from Stage 1
COPY --from=frontend-builder /build/dist/ ./dist/

# Set defaults to Paper Trading Mode
ENV TRADING_MODE=paper
ENV PORT=10000

# Start FastAPI server using uvicorn binding to host and $PORT
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}
