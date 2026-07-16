# Use an official Python runtime as a parent image
FROM python:3.11-slim-bullseye

COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /bin/

# Set the working directory in the container
WORKDIR /MoneyPrinterTurbo

ENV PYTHONPATH="/MoneyPrinterTurbo" \
    PATH="/MoneyPrinterTurbo/.venv/bin:$PATH" \
    MPT_CONFIG_FILE="/MoneyPrinterTurbo/config/config.toml"

# 本地用户默认继续优先使用国内镜像；GitHub Actions 发布 GHCR 镜像时使用 default，
# 避免海外 runner 访问国内镜像过慢导致镜像发布长时间卡住。
ARG DOCKER_BUILD_MIRROR=china

# Install system dependencies with retry logic
RUN if [ "$DOCKER_BUILD_MIRROR" = "china" ]; then \
        echo "deb http://mirrors.aliyun.com/debian bullseye main" > /etc/apt/sources.list && \
        echo "deb http://mirrors.aliyun.com/debian-security bullseye-security main" >> /etc/apt/sources.list; \
    else \
        echo "Using default Debian mirrors"; \
    fi && \
    ( \
        for i in 1 2 3; do \
            echo "Attempt $i: installing system dependencies"; \
            apt-get update && apt-get install -y --no-install-recommends \
                git \
                gosu \
                ffmpeg && break || \
            echo "Attempt $i failed, retrying..."; \
            if [ "$DOCKER_BUILD_MIRROR" = "china" ] && [ $i -eq 3 ]; then \
                echo "Aliyun mirror failed, switching to Tsinghua mirror"; \
                sed -i 's/mirrors.aliyun.com/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list && \
                sed -i 's/mirrors.aliyun.com\/debian-security/mirrors.tuna.tsinghua.edu.cn\/debian-security/g' /etc/apt/sources.list && \
                ( \
                    apt-get update && apt-get install -y --no-install-recommends \
                        git \
                        gosu \
                        ffmpeg || \
                    ( \
                        echo "Tsinghua mirror failed, switching to default Debian mirror"; \
                        sed -i 's/mirrors.tuna.tsinghua.edu.cn/deb.debian.org/g' /etc/apt/sources.list && \
                        sed -i 's/mirrors.tuna.tsinghua.edu.cn\/debian-security/security.debian.org/g' /etc/apt/sources.list; \
                        apt-get update && apt-get install -y --no-install-recommends \
                            git \
                            gosu \
                            ffmpeg; \
                    ); \
                ); \
            fi; \
            sleep 5; \
        done \
    ) && rm -rf /var/lib/apt/lists/*

# Install the exact locked runtime environment before copying application sources.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Now copy the rest of the codebase into the image
COPY . .

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin mpt \
    && mkdir -p /MoneyPrinterTurbo/config /MoneyPrinterTurbo/storage \
    && chown -R mpt:mpt /MoneyPrinterTurbo/config /MoneyPrinterTurbo/storage \
    && chmod +x /MoneyPrinterTurbo/docker-entrypoint.sh

ENTRYPOINT ["/MoneyPrinterTurbo/docker-entrypoint.sh"]

# Expose the port the app runs on
EXPOSE 8501

# Command to run the application
# Listen on all interfaces only inside the container. Host port mappings remain localhost-only.
CMD ["streamlit", "run", "./webui/Main.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.serverAddress=127.0.0.1", "--server.enableCORS=True", "--browser.gatherUsageStats=False", "--client.toolbarMode=minimal", "--logger.hideWelcomeMessage=True", "--server.showEmailPrompt=False"]

# 1. Build the Docker image using the following command
# docker build -t moneyprinterturbo .

# 2. Run the Docker container using the following command
## For Linux or MacOS:
# docker run -v $(pwd)/config:/MoneyPrinterTurbo/config -v $(pwd)/storage:/MoneyPrinterTurbo/storage -p 127.0.0.1:8501:8501 moneyprinterturbo
## For Windows:
# docker run -v ${PWD}/config:/MoneyPrinterTurbo/config -v ${PWD}/storage:/MoneyPrinterTurbo/storage -p 127.0.0.1:8501:8501 moneyprinterturbo
