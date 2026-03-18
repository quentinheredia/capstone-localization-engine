FROM python:3.11-slim AS builder

# Build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git && \
    rm -rf /var/lib/apt/lists/*

# Python deps (pybind11 headers + runtime libs)
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY . .

# Build the C++ pybind11 module
RUN mkdir -p build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release \
             -Dpybind11_DIR=$(python3 -m pybind11 --cmakedir) && \
    cmake --build . --parallel $(nproc)

# ── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libstdc++6 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages

WORKDIR /app
COPY --from=builder /app/src_python ./src_python
COPY --from=builder /app/config.yaml ./config.yaml
# .env is NOT baked into the image — pass it at runtime via --env-file
# docker run --env-file ../.env  (or set vars in your CI environment)

EXPOSE 8000

CMD ["python", "src_python/app.py"]
