# VPS deployment (Ubuntu 24.04)

Two services on the same box: the agent writes `runs/`, the dashboard reads it. Nothing is
exposed on the public internet: the dashboard binds to localhost and Tailscale serves it
over HTTPS to your phone only.

```bash
# 1. code + venv
sudo useradd -r -m -s /usr/sbin/nologin hl
sudo git clone <repo> /opt/hl-agent && sudo chown -R hl:hl /opt/hl-agent
sudo -u hl python3 -m venv /opt/hl-agent/.venv
sudo -u hl /opt/hl-agent/.venv/bin/pip install -e "/opt/hl-agent[web]"
sudo -u hl cp /opt/hl-agent/config/settings.example.toml /opt/hl-agent/config/settings.toml
#    → edit [account] address, [network] name

# 2. secrets (typed on the server, never pasted in a chat)
sudo mkdir -p /etc/hl-agent && sudo chmod 700 /etc/hl-agent
sudo nano /etc/hl-agent/env        # HL_AGENT_PRIVATE_KEY=0x...  HL_AGENT_WEB_TOKEN=...
sudo chmod 600 /etc/hl-agent/env

# 3. units (edit 0xTRADER in hl-agent.service first)
sudo cp /opt/hl-agent/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hl-agent-web
sudo -u hl bash -c 'set -a; . /etc/hl-agent/env; cd /opt/hl-agent && .venv/bin/hl-agent status'   # agent key authorised?
sudo systemctl enable --now hl-agent

# 4. Tailscale: HTTPS on your tailnet only
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo tailscale serve --bg 8080                 # https://<vps>.<tailnet>.ts.net → :8080
```

Install Tailscale on the iPhone, sign in with the same account, open the `.ts.net` URL in
Safari, enter the token once (stored as a cookie for 90 days), then Share → **Add to Home
Screen**. Logs: `journalctl -u hl-agent -f`. Stop trading from the phone (Stop button) or
`touch /opt/hl-agent/runs/copy-live/STOP`.

Since the dashboard can also launch backtests, walk-forwards, fetches and live runs as
subprocesses of the `hl-agent-web` service, it needs the same `/etc/hl-agent/env`
(already the case in `hl-agent-web.service`) and the `hl` user must be able to write
`runs/` and the data cache. If you keep the Senpi checkout on the box, add its path to
`[data] strategy_dirs` in `settings.toml`. A live run started from the phone is a child of
the web service: `systemctl restart hl-agent-web` kills it (a Stop first is cleaner).
