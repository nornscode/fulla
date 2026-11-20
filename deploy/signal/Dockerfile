# The Signal side of Fulla: signal-cli, kept running, behind a small REST
# API, plus the forwarder (forward.py) that turns its receive stream into
# signed POSTs to Fulla. It holds Fulla's Signal account (keys under the
# mounted volume) and nothing else. Fulla talks to it over Fly's private
# network; it is never public. Pinned; bump monthly, since old signal-cli
# releases stop working when Signal changes its servers.
FROM bbernhard/signal-cli-rest-api:0.101

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-websockets \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Upstream starts its services with s6-overlay, whose /init insists on
# being PID 1. On Fly it never is (Fly's init is), so run.py starts the
# same two processes plus the forwarder itself and restarts what dies.
COPY forward.py run.py /usr/local/bin/
USER signal-api
ENTRYPOINT ["python3", "/usr/local/bin/run.py"]
