# Use an official Debian image as the base image
FROM debian:bullseye-20241111-slim

# Set non-interactive mode for apt-get
ENV DEBIAN_FRONTEND=noninteractive

# Install necessary packages: gnupg, vim, parallel, g++, python3 (needed for udocker)
RUN apt-get update -y  \
    && apt-get install -y \
        gnupg2 \
        vim \
        parallel \
        g++ \
        wget \
        python3 \
        curl \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create python symlink for udocker compatibility
RUN ln -sf /usr/bin/python3 /usr/bin/python

# Add Docker's official GPG key:
RUN apt-get update
RUN install -m 0755 -d /etc/apt/keyrings
RUN curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
RUN chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository to Apt sources:
RUN echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update
RUN apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Copy the local keys into the Docker image
COPY keys /keys

# Import GPG keys (continue on error - some keys may be optional or corrupted)
RUN for key in /keys/*.asc; do \
        if [ -f "$key" ]; then \
            gpg --import "$key" || echo "Warning: Failed to import $key"; \
        fi; \
    done || true

# Update the package lists and install GridEngine packages
RUN apt-get update -y && apt-get install -y gridengine-exec gridengine-master gridengine-qmon

# Create a new user 'application' and set password
RUN useradd -m application \
    && echo "application:application" | chpasswd

# Note: Docker group GID modification and user addition will be done at runtime
# (like Harbor image) for flexibility. The docker group is created by docker-ce installation.
# Runtime command should include:
#   groupmod -g $DOCKER_GROUP docker && usermod -aG docker application

# Install udocker
# Download and extract udocker
RUN curl -L https://github.com/indigo-dc/udocker/releases/download/1.3.16/udocker-1.3.16.tar.gz -o /tmp/udocker.tgz \
    && mkdir -p /opt/udocker \
    && tar -xzf /tmp/udocker.tgz -C /opt/udocker --strip-components=1 \
    && rm -f /tmp/udocker.tgz

# Create udocker wrapper script (so it works without PYTHONPATH and sets UDOCKER_DIR)
RUN cat > /usr/local/bin/udocker <<'EOF' && chmod +x /usr/local/bin/udocker
#!/bin/sh
export PYTHONPATH=/opt/udocker/udocker
export UDOCKER_DIR=${UDOCKER_DIR:-/shared_data/udocker}
exec /opt/udocker/udocker/maincmd.py "$@"
EOF

# Set up udocker directory structure (will be mounted at runtime)
# Create base directory structure
RUN mkdir -p /shared_data/udocker

# Set UDOCKER_DIR environment variable (so udocker knows where to store data)
ENV UDOCKER_DIR=/shared_data/udocker

# Set permissions for application user to use udocker
RUN chown -R application:application /opt/udocker \
    && chown -R application:application /shared_data/udocker

# Add setup script and set permissions
ADD setup_gridengine.sh /usr/local/bin/setup_gridengine.sh
ADD setup_submitter.sh  /usr/local/bin/setup_submitter.sh
ADD modify_config.sh  /usr/local/bin/modify_config.sh
ADD modify_sched_config.sh  /usr/local/bin/modify_sched_config.sh
ADD run_sched_logger.sh  /usr/local/bin/run_sched_logger.sh
RUN chmod +x /usr/local/bin/setup_gridengine.sh
RUN chmod +x /usr/local/bin/setup_submitter.sh
RUN chmod +x /usr/local/bin/modify_config.sh
RUN chmod +x /usr/local/bin/modify_sched_config.sh
RUN chmod +x /usr/local/bin/run_sched_logger.sh

# Note: submit_hosts.list will be created at runtime (like Harbor image)
# Runtime command should include:
#   echo "$SUBMITTER_HOSTNAME" > /home/application/submit_hosts.list

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]

