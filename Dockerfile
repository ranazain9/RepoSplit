FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . /app

# Install RepoSplit package in editable mode
RUN pip install --no-cache-dir -e .

ENV HOST=0.0.0.0
ENV PORT=8765
ENV REPOSPLIT_SKIP_LIVE=1

EXPOSE 8765

CMD ["sh", "-c", "reposplit serve --host 0.0.0.0 --port ${PORT}"]
