# Shared dashboard

Permanent URL: https://consistently-not-stupid.sgunturi.workers.dev

Cloudflare Workers serves the dashboard and stores the latest snapshot in a SQLite-backed Durable Object. The Python paper engine and its authoritative ledger run on `chitti-rc-personal` in `/opt/cst`, under the dedicated `cst` system user. The Mac services are disabled. `tools/publish_dashboard.py` publishes selected dashboard fields every five seconds and relays dashboard commands to the server’s loopback API. If publication stops for 30 seconds, the page shows stale data and refuses new commands. Cloudflare hosts the interface; systemd keeps the Python engine running independently of the Mac.

Anyone with the URL can view the account and use the same controls as the local dashboard, per the owner's request. Browser mutations require the shared dashboard's CSRF token and reject foreign origins. The local CSRF token never leaves the engine host. Publication and command pickup use a separate Cloudflare secret. Remote commands expire after one minute and are reserved in the local ledger before execution, preventing a lost acknowledgment from repeating a reset or knob change. A crash after reservation can leave a command unexecuted; submitting a new action is the recovery.

## Update the deployment

```sh
python3 cloudflare/build.py
npx wrangler deploy --config cloudflare/wrangler.jsonc
```

The build copies only HTML, JavaScript and CSS. It fingerprints assets so an update loads the matching scripts. Reusing the Worker name preserves the URL.

For a fresh machine, create `data/cloudflare-publisher.json` with `url` and a random `token`, restrict its permissions to 0600, and install that same token as the Worker's `PUBLISH_TOKEN` secret using `wrangler secret put`. This ignored file is read by the publisher; do not commit it.

## Server operations

The engine listens only on `127.0.0.1:8765`; the publisher uses `CST_LOCAL_URL` to reach it. No new public port is exposed. Both services start at boot and restart on failure.

```sh
ssh chitti-rc-personal 'sudo systemctl status cst-paper cst-dashboard'
ssh chitti-rc-personal 'sudo journalctl -u cst-paper -u cst-dashboard --since "10 minutes ago"'
ssh chitti-rc-personal 'sudo systemctl restart cst-paper cst-dashboard'
```

The authoritative ledger is `/opt/cst/data/book.sqlite`. `cst-backup.timer` creates verified SQLite backups daily and keeps seven days. These backups are on the same server; the migration checkpoint also remains on the Mac. Backups are not replicated off-site automatically.

Service definitions are in `deploy/systemd/`. Deployment uses the current source from `main`, with credentials supplied separately as mode-0600 files. Only the model credential and dashboard publisher token were migrated; Kalshi signing credentials were not copied. The migrated account remains in paper mode.

For future engine updates, transfer reviewed source into `/opt/cst`, preserve `.env` and `data/`, install dependencies using `/opt/cst/.venv/bin/pip`, run tests as `cst`, then restart the engine and publisher. Asset changes also need the Cloudflare build and deploy above. Never start a second engine from the Mac’s old ledger.

To roll back hosting, stop both server services first, take a consistent backup of the server’s current ledger, transfer it to the Mac, and only then reinstall the Mac services. Do not restore the pre-migration ledger over newer trades.
