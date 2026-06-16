FROM python:3.11-slim-bullseye

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Set the working directory
WORKDIR /app

# Install system dependencies (curl-cffi needs libcurl for some environments)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies directly
RUN pip install --no-cache-dir \
    curl-cffi~=0.15.0 \
    loguru~=0.7.3 \
    orjson~=3.11.7 \
    pydantic~=2.12.5 \
    fastapi \
    uvicorn \
    httpx

# Copy the core library code and app files
COPY src /app/src
COPY dashboard_server.py dashboard.html index.html api_server.py /app/

# Expose the port uvicorn runs on
EXPOSE 8000

# Run the FastAPI server using python
CMD ["python", "api_server.py"]
