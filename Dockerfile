FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash git ripgrep ca-certificates procps tini curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.5
WORKDIR /opt/ascend
COPY pyproject.toml uv.lock LICENSE ./
COPY src ./src
RUN uv sync --frozen --extra workspace --no-dev --no-editable
RUN printf '%s\n' 'export PATH=/opt/ascend/.venv/bin:$PATH' > /etc/profile.d/ascend.sh
COPY scripts/prepare_workspace.py /opt/ascend/prepare_workspace.py
RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /workspace && chown agent:agent /workspace
ENV PATH="/opt/ascend/.venv/bin:${PATH}" \
    HOME=/home/agent \
    PYTHONUNBUFFERED=1
LABEL io.delva.dsh-ascend="0.1.0"
USER agent
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
