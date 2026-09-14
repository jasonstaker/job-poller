# job-poller

A small tool I built with Claude to help me keep tabs on internship applications.

It checks a list of company job boards on a schedule and sends a phone notification when
a new software engineering internship or co-op posting shows up, so I hear about one
within the hour instead of a week later.

Python + GitHub Actions + [ntfy](https://ntfy.sh). No database, no server.

## Running it

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt      # .venv/Scripts/pip on Windows

python -m pytest -q            # tests
python poller.py --probe       # check every board is reachable
python poller.py --dry-run     # full run, no notifications, no state written
python poller.py               # a real run
```

Notifications need an `NTFY_TOPIC` environment variable, set as a repository secret in
Actions.

## Layout

| File | Role |
|---|---|
| `filters.py` | Which job titles count as a software internship |
| `handlers.py` | One fetch function per job-board platform |
| `poller.py` | Entry point: fetch, diff, filter, notify |
| `notify.py` | ntfy push |
| `sources.json` | The company list, plus `manual_check` for boards that cannot be polled |
| `state/seen.json` | Every job ID seen so far, so nothing notifies twice |

Engineering notes -- which boards cannot be polled, and where the build spec turned out
to be wrong -- are in [NOTES.md](NOTES.md).
