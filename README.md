# fusion-monitor

A read-only background daemon that watches the [Fusion Festival](https://www.fusion-festival.de) ticket transfer forum and sends an alert the moment someone posts a genuine ticket offer.

Fusion sells out in minutes. The secondary market lives on a German-language phpBB forum with hundreds of posts per hour. This tool filters the noise and gets you there first.

**Strictly read-only — it never logs in, posts, replies, or bumps anything.**

---

## How it works

- Polls `forum.fusion-festival.de/viewforum.php?f=82` every ~5 minutes with random jitter
- Parses thread titles for German offer keywords (`biete`, `abzugeben`, `verkaufe`, `verschenke`, …)
- Filters out false positives:
  - Pure search posts (`suche`, `gesucht`)
  - Mixed-intent posts ("searching for ticket, offering a ride in return")
  - Old threads bumped to the top by new comments
- On a new match: fires a macOS desktop notification **and** sends an email to a configurable recipient list
- Keeps a local JSON state file so the existing backlog is silently recorded on first run — no alert spam

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/sammorgandk/fusion-monitor.git
cd fusion-monitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. Get a Gmail App Password

Regular Gmail passwords are blocked by Google for SMTP. You need an App Password:

1. Go to [myaccount.google.com](https://myaccount.google.com) → **Security**
2. Enable **2-Step Verification** if not already on
3. Search for **"App passwords"** → create one (name it anything)
4. Copy the 16-character password

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env`:

```
FUSION_SMTP_PASSWORD=your-16-char-app-password
```

The `.env` file is gitignored and never leaves your machine.

### 4. Configure recipients and sender

Edit the `CONFIG` block at the top of `fusion_monitor.py`:

```python
EMAIL_FROM = "you@gmail.com"
EMAIL_TO   = ["you@gmail.com", "friend@gmail.com"]
```

---

## Usage

### Dry run — sanity-check the filter before going live

```bash
venv/bin/python3 fusion_monitor.py --dry-run
```

Fetches the forum once and prints every thread with a label (`ALERT`, `MIXED`, `OLD`, or blank) so you can see exactly what would and wouldn't trigger an alert.

### Run manually

```bash
venv/bin/python3 fusion_monitor.py
```

### Run in the background (macOS launchd)

Install as a launch agent so it starts at login and restarts automatically if it crashes:

```bash
# Edit the plist to point to your venv and script paths if different, then:
cp com.sammorgandk.fusion-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sammorgandk.fusion-monitor.plist
```

Check it's running:

```bash
launchctl list | grep fusion
tail -f ~/Library/Logs/fusion-monitor.log
```

Stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.sammorgandk.fusion-monitor.plist
```

---

## Configuration

All tunable values are in the `CONFIG` block at the top of `fusion_monitor.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OFFER_KEYWORDS` | `biete`, `abzugeben`, … | German words indicating a ticket offer |
| `SEARCH_KEYWORDS` | `suche`, `gesucht` | Words that mean someone is searching |
| `POLL_INTERVAL_S` | `300` | Base poll interval in seconds |
| `JITTER_MAX_S` | `60` | Max random jitter added to each interval |
| `MIN_INTERVAL_S` | `120` | Hard floor — never polls faster than this |

---

## Project structure

```
fusion-monitor/
├── fusion_monitor.py                    # main script
├── requirements.txt
├── .env.example                         # template — copy to .env and fill in password
├── .gitignore                           # excludes .env, state file, venv
└── com.sammorgandk.fusion-monitor.plist # launchd agent definition
```

---

## Requirements

- Python 3.11+
- macOS (for desktop notifications and launchd; email works anywhere)
- A Gmail account with an App Password
