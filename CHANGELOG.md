# Changelog

All notable changes to this project are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- (nothing yet)

## [0.1.0] — 2026-11-12 (proposed initial public release)

### Added

- Read-only CLI (`python -m src.main`) for Telegram **supergroup** research: discover groups, sync messages locally, search, and export Markdown.
- **Forum-enabled supergroups:** list topics, windowed `sync-group` across all topics, per-topic `sync` / `ingest`, General-topic diagnostics.
- **Ordinary single-stream supergroups:** `sync-group` syncs the main chat via `GetHistory`, stored as synthetic topic **Main** (ID `0`).
- **SQLite archive** with incremental sync checkpoints, overlap re-fetch, and deduplication by `(chat_id, message_id)`.
- **Sync windows:** `--months` (default 6) or `--days` for bounded backfill; policy stored in checkpoints so pilots can expand to longer windows later.
- **Offline tools:** `sync-status`, FTS5 `search`, `inspect`, `message-context`, `export-topic` (batch and per-topic, including `after-ID` incremental export).
- Exports include Telegram **message IDs** and **links** where available, plus short analysis guidance for manual LLM use (no in-app LLM calls).
- **`ask`** command builds offline evidence packages from the local archive only.
- Local evidence housekeeping: `storage-usage`, `cleanup-evidence`.
- MIT licensed; local-first credentials, sessions, and data paths (git-ignored).

### Known limitations

- **Broadcast channels** and legacy **basic groups** are not supported (`sync-group` requires a supergroup).
- **`topics`** is for forum supergroups only; non-forum groups use `sync-group` directly.
- Standalone **`sync` / `ingest`** target forum topics; single-stream groups rely on **`sync-group`**.
- No automated LLM integration — analysis is manual (export → your assistant).
- Telegram rate limits (`FloodWait`) and membership rules still apply; message links may require group access to open.
- A very short **`--days`** window may fetch zero messages if the group was quiet since the UTC cutoff (widen with `--days 2` or `--months`).
