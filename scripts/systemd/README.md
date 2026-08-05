# systemd units

`nocturne-kstars.service` starts KStars headless with its own session bus, which
is what the Ekos DBus bridge attaches to (`org.kde.kstars` — see
`backend/nocturne/executor/ekos.py`).

The orchestrator's own unit arrives with M2, when there is a FastAPI application
to run, together with the independent watchdog process of SPEC section 9.5. The
watchdog must not depend on the FastAPI process, so it gets a separate unit with
no `Requires=` on it.

## Installing

```bash
sudo cp scripts/systemd/nocturne-kstars.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nocturne-kstars.service
systemctl status nocturne-kstars.service
```

Edit `User=` and the paths first; the unit assumes the `nocturne` user and a
checkout at `/home/nocturne/nocturne`.
