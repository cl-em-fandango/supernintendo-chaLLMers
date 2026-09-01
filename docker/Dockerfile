FROM registry.fedoraproject.org/fedora-minimal:latest

# Install python, git, node+pi (the model CLI the harness drives), iptables, iproute, ca-certificates via microdnf
RUN microdnf install -y python3 python3-pip git nodejs npm iptables iproute shadow-utils ca-certificates coreutils && \
    microdnf clean all && \
    npm install -g --silent @earendil-works/pi-coding-agent && \
    npm cache clean --force

# Create runtime user
RUN useradd -m -u 1000 -s /bin/bash harnessuser

# Prepare directories and permissions
RUN mkdir -p /opt/harness-frozen /workspace /entrypoint-scripts /home/harnessuser/.pi/agent /root/.pi/agent && \
    chown -R harnessuser:harnessuser /opt/harness-frozen /workspace /home/harnessuser

# Copy current frozen harness codebase into container
COPY --chown=harnessuser:harnessuser . /opt/harness-frozen

# Copy e2e agent config into ~/.pi/agent for harnessuser and root
RUN cp -r /opt/harness-frozen/e2e/e2e-test-agent/* /home/harnessuser/.pi/agent/ && \
    cp -r /opt/harness-frozen/e2e/e2e-test-agent/* /root/.pi/agent/ && \
    chmod -R 777 /home/harnessuser /root/.pi /workspace

# Copy firewall setup script and entrypoint
COPY docker/init-firewall.sh /entrypoint-scripts/init-firewall.sh
COPY docker/entrypoint.sh /entrypoint-scripts/entrypoint.sh
RUN chmod +x /entrypoint-scripts/init-firewall.sh /entrypoint-scripts/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/opt/harness-frozen
ENV HOME=/home/harnessuser
ENV PI_CODING_AGENT_DIR=/home/harnessuser/.pi/agent

WORKDIR /workspace

ENTRYPOINT ["/entrypoint-scripts/entrypoint.sh"]
CMD ["tail", "-f", "/dev/null"]
