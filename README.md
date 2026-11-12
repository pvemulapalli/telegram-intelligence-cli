# telegram-intelligence

Read-only Telegram forum research CLI — sync messages into local SQLite, search with FTS5, and export source-linked Markdown for optional manual AI analysis.

**Local-first:** credentials, sessions, and message archives stay on your machine. This project does not send messages to Telegram, does not execute trades, and does not call LLM APIs.

## Privacy and acceptable use

- Use only Telegram groups you are **authorized** to access.
- Follow [Telegram's Terms of Service](https://telegram.org/tos) and each community's rules.
- Do **not** publish or redistribute private group messages, member identities, or exports without permission.
- Keep `.env`, files under `sessions/`, and `data/` private and out of version control.

## Requirements

- Python **3.12+**
- Telegram API credentials from [my.telegram.org/apps](https://my.telegram.org/apps)
- A Telegram user account that can read the target **forum-enabled supergroup**

## Setup

```bash
git clone https://github.com/pvemulapalli/telegram-intelligence-cli
cd telegram-intelligence-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: TELEGRAM_API_ID and TELEGRAM_API_HASH
```

The first command that talks to Telegram will prompt for login (phone number, verification code, and 2FA if enabled). A session file is stored under `sessions/` (git-ignored).

## Quick start

### 1. List groups you can access

```bash
python -m src.main groups
```

Copy the **Chat ID** for your forum supergroup (negative numbers are normal).

Example output (synthetic):

| Name            |       Chat ID | Type       | Forum |
|-----------------|--------------:|------------|-------|
| Example Forum   | -1001234567890 | supergroup | yes   |

### 2. List forum topics

```bash
python -m src.main topics --chat-id -1001234567890
```

### 3. Sync all topics (windowed backfill, then incremental)

```bash
python -m src.main sync-group \
  --chat-id -1001234567890 \
  --months 6
```

- **`--months`** — calendar months of history to backfill from run time (default **6**).
- After each topic finishes its window (`Window=yes` in `sync-status`), later runs use **incremental** mode (new messages plus a small ID overlap).
- Optional: `--overlap 10`, `--topic-delay 1.0`.

When any topic has new inserts, the summary prints a suggested **`export-topic --topics`** command using the **actual synced topic titles**.

### 4. Check sync status (offline)

```bash
python -m src.main sync-status
```

SQLite only — no Telegram network calls.

### 5. Export topics to Markdown (offline)

Latest **N** messages per topic:

```bash
python -m src.main export-topic \
  --chat-id -1001234567890 \
  --topics "Announcements:50|Questions:30|Support:20"
```

Incremental export (messages newer than a boundary ID from a prior export):

```bash
python -m src.main export-topic \
  --chat-id -1001234567890 \
  --topics "Announcements:after-12345|Questions:after-54321"
```

Topic names are resolved from SQLite titles (exact, normalized, or unique prefix). Each `TopicName:after-ID` boundary is **per topic** (highest message ID from your last successful export of that topic).

Files are written under `data/evidence/topics/` (unique filenames; existing files are not overwritten).

Single topic:

```bash
python -m src.main export-topic \
  --chat-id -1001234567890 \
  --topic "Announcements" \
  --limit 60
```

### 6. Optional AI analysis

Upload an export to your preferred assistant. Each Markdown file includes analysis instructions and Telegram message IDs for citation. You are responsible for what you upload and for respecting group privacy.

## Search and inspect (offline)

```bash
python -m src.main search \
  --chat-id -1001234567890 \
  --query "onboarding" \
  --limit 10

python -m src.main inspect \
  --chat-id -1001234567890 \
  --topic-id 100 \
  --limit 10
```

## How group selection works

There is **no default chat** in code. Every sync/export command requires **`--chat-id`**, which you obtain from `groups`. The application lists only dialogs your logged-in account can access.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Layout

| Path | Purpose |
|------|---------|
| `src/` | CLI and sync/export logic |
| `tests/` | Offline unit tests |
| `data/telegram.db` | Local archive (git-ignored) |
| `data/evidence/` | Exports (git-ignored) |
| `sessions/` | Telegram session (git-ignored) |
| `.env` | API credentials (git-ignored) |

## Other commands

| Command | Purpose |
|---------|---------|
| `groups`, `topics` | Discover groups and forum topics (Telegram API) |
| `sync` | Single-topic sync |
| `export-topic` | Export stored messages to Markdown |
| `search`, `message-context` | FTS search and thread context (SQLite) |
| `diagnose-general` | General topic (ID 1) classification sample |
| `storage-usage`, `cleanup-evidence` | Local evidence package housekeeping |

Read-only toward Telegram: no outbound messages. No automated LLM integration.
