FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake ninja-build g++ libjsoncpp-dev uuid-dev zlib1g-dev \
    libssl-dev libpq-dev libhiredis-dev libbrotli-dev && rm -rf /var/lib/apt/lists/*
ARG DROGON_VERSION=v1.9.14
RUN git clone --depth 1 --branch "$DROGON_VERSION" --recurse-submodules --shallow-submodules \
    https://github.com/drogonframework/drogon.git /tmp/drogon && \
    cmake -S /tmp/drogon -B /tmp/drogon/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CXX_STANDARD=17 -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DBUILD_CTL=OFF && \
    cmake --build /tmp/drogon/build -j 2 && cmake --install /tmp/drogon/build
WORKDIR /src
COPY backend /src/backend
RUN cmake -S backend -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build -j 2 && ctest --test-dir build --output-on-failure
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjsoncpp25 libssl3t64 libpq5 libhiredis1.1.0 libbrotli1 zlib1g uuid-runtime \
    ca-certificates curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /src/build/devdocqa /app/devdocqa
COPY --from=build /usr/local/lib/ /usr/local/lib/
COPY web /app/web
RUN ldconfig && useradd --create-home --uid 10001 docqa && \
    mkdir -p /data/documents && chown -R docqa:docqa /data /app
USER docqa
EXPOSE 8080
CMD ["/app/devdocqa"]
