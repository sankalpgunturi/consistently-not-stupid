# Shared dashboard

Permanent URL: https://consistently-not-stupid.sgunturi.workers.dev

Cloudflare Workers serves the dashboard and stores the latest snapshot in a SQLite-backed Durable Object. The Python paper engine and its authoritative ledger still run on the Mac. `tools/publish_dashboard.py` publishes selected dashboard fields every five seconds and relays dashboard commands to the local API. If publication stops for 30 seconds, the page shows stale data and refuses new commands. This deployment does not move the trading engine into Workers.

Anyone with the URL can view the account and use the same controls as the local dashboard, per the owner's request. Browser mutations require the shared dashboard's CSRF token and reject foreign origins. The local CSRF token never leaves the Mac. Publication and command pickup use a separate Cloudflare secret. Remote commands expire after one minute and are reserved in the local ledger before execution, preventing a lost acknowledgment from repeating a reset or knob change. A crash after reservation can leave a command unexecuted; submitting a new action is the recovery.

## Update the deployment

```sh
python3 cloudflare/build.py
npx wrangler deploy --config cloudflare/wrangler.jsonc
```

The build copies only HTML, JavaScript and CSS. It fingerprints assets so an update loads the matching scripts. Reusing the Worker name preserves the URL.

For a fresh machine, create `data/cloudflare-publisher.json` with `url` and a random `token`, restrict its permissions to 0600, and install that same token as the Worker's `PUBLISH_TOKEN` secret using `wrangler secret put`. This ignored file is read by the publisher; do not commit it.

```sh
.venv/bin/python tools/dashboard_service.py install
.venv/bin/python tools/dashboard_service.py status
.venv/bin/python tools/dashboard_service.py restart
```

The launch agent restarts the publisher after failures and at login. The paper runner has its own launch agent (`tools/paper_service.py`). Both must run for remote controls and fresh account updates. Logs stay in `data/logs/dashboard.log` and `data/logs/runner.log`.
