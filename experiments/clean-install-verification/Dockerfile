FROM qingniao-ubuntu-base:2404
# Base image note: built via `docker import` of the official
# ubuntu-base-24.04.3-base-amd64.tar.gz (cdimage.ubuntu.com) because the
# local Docker daemon's configured registry mirrors are unreachable; see
# README.md in this directory. Only a minimal Ubuntu 24.04 rootfs is used.
ENV DEBIAN_FRONTEND=noninteractive
# Base environment only: Python 3.12 (satisfies requires-python >=3.12) plus
# the tools the install docs need. The application itself is installed from
# the release wheel below, never from a source checkout.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}" \
    QING_DOCKER_TOKEN=docker-fixture-token
# Install from the release artifact exactly as the public docs describe.
COPY qingniao.whl /tmp/qingniao_gateway-0.1.0-py3-none-any.whl
RUN uv tool install /tmp/qingniao_gateway-0.1.0-py3-none-any.whl
RUN qing --version
COPY install_check.py /tmp/install_check.py
CMD ["/root/.local/share/uv/tools/qingniao-gateway/bin/python", "/tmp/install_check.py"]
