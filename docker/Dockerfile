FROM registry.fedoraproject.org/fedora-minimal:latest

# Install python, git, node+pi (the model CLI the harness drives), iptables, iproute, ca-certificates via microdnf
RUN microdnf install -y python3 git nodejs npm iptables iproute shadow-utils ca-certificates && \
    microdnf clean all && \
    npm install -g --silent @earendil-works/pi-coding-agent && \
    npm cache clean --force

# Create unprivileged runtime user
RUN useradd -m -u 1000 -s /bin/bash harnessuser

# Prepare frozen code directory and workspace
RUN mkdir -p /opt/harness-frozen /workspace /entrypoint-scripts && \
    chown -R harnessuser:harnessuser /opt/harness-frozen /workspace

# Copy current frozen harness codebase into container
COPY --chown=harnessuser:harnessuser . /opt/harness-frozen

# Copy firewall setup script and entrypoint
COPY docker/init-firewall.sh /entrypoint-scripts/init-firewall.sh
COPY docker/entrypoint.sh /entrypoint-scripts/entrypoint.sh
RUN chmod +x /entrypoint-scripts/init-firewall.sh /entrypoint-scripts/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/opt/harness-frozen

WORKDIR /workspace

ENTRYPOINT ["/entrypoint-scripts/entrypoint.sh"]
CMD ["python3", "/opt/harness-frozen/harness.py", "status"]
