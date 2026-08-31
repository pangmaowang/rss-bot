# Agent Changelog

For coordination between agents: record changes that are **not committed yet**,
newest first. Append your own entry, do not rewrite or revert someone else's.
Committed history lives in `git log`, so an entry can be deleted once its work
is committed. This is not a release changelog.

Entry format: one heading line (date and what you did), then the files you
touched, how you verified it, and any risk you left behind.

## 2026-08-31 — reading starts at the top; b exports from the feed list too

- `reader.py` `StreamScreen.on_mount` / `grab.py` `read_zh`: dropped the
  `VerticalScroll.anchor()` — it followed the stream to the bottom like a chat
  log, but articles are read from the top; the view now stays put while the
  stream fills in below.
- `reader.py`: export moved to `Reader.export_selected()` (the selection lives
  on the app, so exporting it does too). `FeedsScreen` gains the `b` binding —
  a selection accumulated across feeds no longer strands when you back out —
  and says "Nothing selected" on an empty press. `ArticlesScreen.action_export`
  keeps the cursor-row fallback by ticking it first, so a failed start now
  leaves it selected for a retry like any other row.
- Docs: README key tables, design/reader.md (reading view, filter section, new
  Selection and export section).
- Pre-push review round (3 grouped angles) found 8 issues, all fixed here:
  a mid-stream producer error now also toasts (`stream_md`), since the
  in-document warning lands below the fold once nothing anchors to the bottom;
  `export_selected` returns None with "Nothing selected" inside it, so both
  screens give the same feedback and the bool's OSError double-meaning is gone;
  `FeedsScreen` gained `u` (a failed-start orphan whose feed no longer shows
  was otherwise unclearable); parallel guids/items lists became one
  `list(selected.items())`; the duplicated rationale docstrings were trimmed;
  the scroll test got completion + scrollability asserts and lost its
  redundant double-wait; the cursor-fallback and feed-list `u` paths got
  runnable checks.
- Verified: each new check failed before its code (toast: AssertionError [],
  feed-b: exported [], scroll: Offset(y=227) with anchor restored), suite +
  py_compile + `git diff --check` pass after.
- Risk: none known; no storage or fetching changes.

## 2026-08-29 — review round: nine findings fixed before commit (in 31bb709)

A 9-angle review (line-scan, removed-behavior, cross-file, reuse,
simplification, efficiency, architecture, conventions, and the background
export path) over everything below. Fixed:

- `grab.py` `api_key()`: the placeholder guard now covers the environment path
  too (`source .env` exports it verbatim), via one shared `usable()`; a
  placeholder in the environment falls through to the `.env` file.
  `.env.example` now ships `GRAB_API_KEY=` empty, and the test copies the real
  example file so guard and example cannot drift apart.
- `grab.py` `grab()`: exclusive-create (`open("x")`) loop replaces
  `unique()` + `write_text` — two parallel `--bg` exports of the same title
  could race the existence check and silently truncate each other.
- `grab.py` header rebuild: the regex now eats at most one H1, so a translated
  body that opens with its own heading keeps it (and cannot donate the
  filename). The reordered-header case still works.
- `grab.py` `extract()`: read retries back to 1 (refresh keeps 0) — a
  mid-response drop counts as a read error and a one-shot export loses the
  article; documented in design/reader.md Known trade-offs.
- `grab.py` failure notifications: title capped at 60 chars so it cannot crowd
  the error reason out of the 120-char body.
- `reader.py`: `/` now always starts a blank search (old text concatenated);
  Esc resets the filter and reloads synchronously, so an immediate `a`/`b`
  cannot act on the stale filtered rows.
- `design/reader.md`: the filter section no longer overpromises — `a` ticks
  the visible rows, but the selection stays app-wide for `b` and `u`.
- `config.toml`: antirez switched to https (was the only cleartext feed).
- `test_grab.py`: a runnable check per fix, plus a vacuous-pass guard on the
  first filter assertion.
- Verified: full suite, py_compile, `git diff --check`; the api_key/regex/race
  fixes were each demonstrated failing before the change by the review agents.
- Risk: `o`/export worst-case wait is 2x30s again when a host accepts the
  connection but stalls mid-read — chosen over losing the article.

## 2026-08-29 — `/` title filter in the article list; feed curation (in 31bb709)

- `reader.py` `ArticlesScreen`: `/` opens a one-line Input, each keystroke
  narrows the table. The text splits on spaces and a title must contain every
  word, any order, casefolded (`rust async` finds "Async Rust..."). Enter keeps
  the filter, Esc clears it. The filter gates `load()`, so `self.rows` — and
  with it space / `a` — only see the visible set: `/` + `a` + `b` exports a
  keyword. `check_action` lets only Esc act while the box has focus, because
  left/right are priority bindings and would navigate instead of editing.
- `design/reader.md`: filter section added, `/` removed from "deliberately
  absent" (the doc changed before the code). Both READMEs: key table row,
  ten -> eleven keys.
- `config.toml`: dropped lobste.rs (second aggregator); added 19 feeds — known
  researchers (Karpathy, Chip Huyen, Jay Alammar, Hamel Husain, Ethan Mollick)
  and big-lab research blogs on the `ai` tag; independent names (antirez,
  Mitchell Hashimoto, Pragmatic Engineer, Martin Fowler) and big-company
  engineering blogs (Meta, GitHub, Stripe, Slack, Spotify, Dropbox, Airbnb) on
  `eng`. Every URL live-verified same day (fetch + parse + non-empty guids);
  Uber (HTTP 406/404) and colah (last post 2019) rejected.
- Verified: new UI checks in `test_grab.py` (filter narrows/restores, `a` on
  the filtered view, left-while-typing stays, esc-while-typing) failed before
  the code, suite passes after; live 38-feed refresh x2 into a temp DB: 1153
  items, second round inserts 0, only bair.berkeley.edu fails (their server).
- Risk: none known beyond feed taste; the filter is per-screen state and
  touches no storage.

## 2026-08-26 — two field fixes: key placeholder, retry multiplication (in 31bb709)

- `grab.py` `api_key()`: a copied-but-unfilled `.env.example` leaves
  `GRAB_API_KEY=sk-or-v1-`; that placeholder was sent as a real key and the
  user got `401 Missing Authentication header` instead of the designed
  "Translation needs GRAB_API_KEY" message. The placeholder now reads as no key.
- `reader.py` `fetch()` / `grab.py` `extract()`: `urllib3.request` defaults to
  3 retries, so one dead feed cost 4 x 15s per refresh round (measured 60.4s on
  a bair.berkeley.edu connect timeout) and `o` could hang 4 x 30s. Now
  `Retry(3, connect=0, read=0)` on refresh (extract keeps `read=1`, see the
  review entry above): redirects still follow, hangs cost one timeout
  (measured 18.1s round with the same dead feed).
- Verified: new checks in `test_grab.py` (placeholder -> None; retries kwarg on
  both call sites) failed before the fix, suite passes after; live re-run of a
  20-feed refresh and a piped `--read --zh`.
- Risk: a feed behind a flaky-but-alive network now fails a round instead of
  retrying; the hourly refresh picks it up next round.
