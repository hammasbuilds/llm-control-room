# Review notes, 2026-10-07

Hostile read of `llm-control-room` 0.1.0 through the real HTTP app, then fixes. Severity is what a user
running this on their own machine would face. Every fixed item has a test in `tests/test_security.py`
(unless noted) that fails on the 0.1.0 code.

## Weaknesses found

### Critical

| # | Finding | Evidence | Fix |
|---|---|---|---|
| 1 | **Admin API unauthenticated on the gateway port** unless `LCR_ADMIN_TOKEN` was set, and the UI never told the user. Everything under `/api` (tenant keys, demo keys in `/api/meta`, `/api/reset`, `/api/sandbox/run`) was open to any local process. | `GET /api/meta` returned `demo_keys` with no header. | Token on by default: the launcher generates `admin-token` beside the database and opens the browser at `/#token=`; constant-time compare; 20 failures/minute per client then 429. |
| 2 | **CSRF and DNS rebinding reached `POST /api/sandbox/run`.** Handlers called `request.json()` regardless of `Content-Type`, so a web page could send a `text/plain` "simple" POST (no CORS preflight) and run code with the `subprocess` profile on the owner's machine. A page re-pointing its own hostname at 127.0.0.1 could also *read* the API. | `curl -H 'Content-Type: text/plain' -d '{"code":"..."}' /api/sandbox/run` executed. | `SecurityMiddleware`: Host allow-list, same-origin check on writes, `Sec-Fetch-Site: cross-site` refused on `/api`, JSON `Content-Type` required on any body, 1 MB body cap. The `subprocess` profile additionally needs `allow_unsafe: true` (UI asks with a confirm). |
| 3 | **The `restricted` sandbox could be switched off from inside in six lines.** The audit hook's deny-set lived in the launcher module's globals; `e.__traceback__.tb_frame.f_back` walks to that module and `_DENY.clear()` re-enables `subprocess`. Verified against the 0.1.0 `BOOT`: the program printed `ESCAPED: 42`. | `tests/test_security.py::test_restricted_profile_cannot_be_switched_off_from_inside` | Hook state moved into a closure no frame or module attribute leads to; user code runs in a thread started by the launcher so its frame chain ends before it; `gc.get_objects/get_referrers/get_referents` refused. |

### High

| # | Finding | Fix |
|---|---|---|
| 4 | `restricted` stopped only open/network-connect/Popen: it allowed `os.remove`, `os.rename`, `os.mkdir`, `os.rmdir`, `os.truncate`, `shutil.copyfile`, `os.symlink` outside the workdir (deleting host files), `os.listdir`/`scandir` of any directory, UDP `sendto`/`sendmsg`, `os.kill`, `os.startfile`, `_winapi.CreateProcess`, writing a `.pyd`/`.dll` into the workdir (loadable native code), unbounded memory (3 GB allocated) and unbounded disk (2 GB written). | Path checks on every write/rename/link event and on directory listing, wider deny list, executable-suffix write block, Windows job-object memory cap (Linux `RLIMIT_AS`, not run here), workdir size monitor, 4 concurrent sandbox programs. Probe suite grew from 7 to 12 attacks. |
| 5 | **Budget and rate limit raced.** The check (`SELECT`) and the record (`INSERT`) were separate, so concurrent callers (agent threads, simulator, anything not serialised on the event loop) all passed a budget that fit one request. 12 threads against a budget for 2.5 requests: 12 served. Same for rpm. | In-flight reservation under a lock; `/v1` handlers run in a worker thread. |
| 6 | **A slow upstream froze the whole server.** `async def` endpoints called blocking `handle()`, `run_code()` (up to 60 s), `probe()` and `sim.run()` on the event loop: every other tenant and the admin UI hung. | `run_in_threadpool` for all of them. |
| 7 | **Tenant could steer the mock provider.** `[[mock error=1 quality=-1]]` in a tenant's own system message was read as a directive; sent with a release name it poisons the shared release window and can roll back a healthy canary for every tenant. | Directives are neutralised in tenant text; only the release's own prompt is trusted. |
| 8 | **Deleted tenant's cache and spend leaked to a new tenant with the same name.** | Cache purged on policy change/delete; history re-keyed to `name~deleted`. |
| 9 | **Redaction misses.** Not found: `sk-proj-...` keys (the pattern forbade `-`/`_`), `sk` split by U+200B/U+200D/bidi controls, full-width `ｓｋ-`, base64/percent/hex-encoded keys, PEM bodies (only the `BEGIN` line was redacted), `password=...`, bearer tokens, Stripe/Google/Hugging Face keys, this gateway's own `lcr-` keys, SSN/IBAN/international phone, full-width email. Model output was scanned for secrets only, never PII. | New `guard.py` in front of the vendored (still unmodified) guardrails: NFKC + format-character stripping, one decoding level, wider patterns. Text is returned untouched unless something was redacted, so Urdu with U+200C is not rewritten. |
| 10 | **Injection filter bypasses.** Cyrillic/Greek look-alikes, zero-width characters, spacing and punctuation (`i g n o r e`), `disregard the previous instructions`, base64/percent-encoded instructions, an instruction split across messages, and any text in `assistant`/`tool` roles (only `user` was screened). | Folded, compact and decoded views; screening of every non-system role plus a joined pass. Still a speed bump (paraphrase and other languages pass). |
| 11 | **ReDoS.** The email pattern was quadratic on `a-a-a-...` (and the first draft of the new credential pattern was too): 200 KB of hostile text stalled the server for seconds, on the event loop. | Bounded quantifiers; 200,000-character request cap (413); regression test with a time bound. |

### Medium

| # | Finding | Fix |
|---|---|---|
| 12 | "No prompt stored": the claim held for `calls`, but the agent **goal** (the prompt of a run) was stored verbatim in `runs` and `run_events`, with secrets in it. `feature` and `model` strings were stored unbounded, so a client could park a prompt-sized string in `calls`. | Goal redacted (secrets and PII) and capped at 2000 characters before storage; labels cleaned and capped (64/80). Test dumps every table after every request path (success, blocked, upstream failure, context, playground, agent). |
| 13 | Shadow-version calls cost money but were excluded from the tenant's budget. | Budget counts them. |
| 14 | `/v1/models` listed every model and every release to every tenant, ignoring `allowed_models`. | Filtered per tenant. |
| 15 | A key holder could write unlimited zero-cost refusal rows (blocked, rate-limited, over budget) and grow the SQLite file without bound. | Newest 100,000 refusals kept; spend rows never pruned. |
| 16 | Failed tenant-key guesses were unthrottled (the demo keys are guessable: `lcr-demo-<tenant>`). | Per-client lockout; no demo keys at all when bound off loopback. |
| 17 | 500s from malformed input: a JSON array body, `lcr` not an object, `model` a list, `max_tokens: "x"`, `"NaN"` temperature, `messages` with non-objects. `/api/calls?limit=-1` returned the whole table (SQLite `LIMIT -1`). | 400s with messages; limit clamped to 1..500; `max_tokens` clamped; NaN refused. |
| 18 | Agent limits accepted `NaN`/`inf`/huge values (a run that never stops) and any number of concurrent runs. | Finite, capped limits; at most 8 live runs. |
| 19 | Probe verdicts were right for the wrong reason: `spawn_process` under Docker "stopped" because it launched the *host's* `python.exe` path, which does not exist in the container. | **Open** (see below). The new `hook_tamper` attack reads a host file instead of spawning. |

### Low / honesty

- The README said "Memory limits are not enforced outside Docker on Windows": now enforced by a job object for `restricted`.
- `test_hardened_profile_in_docker_stops_the_attacks` timed out when Docker took 28 s to start a container; wall limit raised in the test.
- UI: all rendered strings go through `esc()`; no XSS found (checked every template that interpolates server data).
- UX: the token prompt was a bare `prompt()` that looped on a wrong token; now the launcher signs the tab in. Tenant policy form lacked the new terms fields (added).

## Fixed vs. left open

**Left open on purpose**

- `restricted` remains an in-process audit hook. It still allows `os.stat`, threads, CPU burn until the wall clock, and anything a system DLL exposes through handles it can already reach. Docker is the boundary.
- `spawn_process` under `hardened` is "stopped" only because the attack launches the host's interpreter path. Process creation inside the container is allowed (`--pids-limit 64`) and is not an escape; the attack should be judged differently for Docker.
- Releases, their SLO windows and rollbacks are global across tenants; there is no per-tenant release ACL.
- No TLS. A non-loopback bind sends the admin token and tenant keys in clear text.
- One shared admin token, no roles, no audit log of admin actions.
- A request can overshoot a budget by the gap between the router's expected completion and the real one.
- Linux `RLIMIT_AS`/`RLIMIT_FSIZE` code path is untested on this machine.

## Product gaps (most valuable next)

1. Per-tenant release access and per-tenant SLO windows.
2. Streaming from providers (today `stream: true` replays a finished answer) with mid-stream budget stop.
3. A key-level admin audit log and roles (read-only dashboard viewer vs. operator).
4. Real-provider price and usage sync (so budgets use billed tokens, not estimates).
5. Persisted release windows so a restart does not reset canary evidence.

## Features added in this pass

- **Per-tenant block and redact terms** (literal, never regex, so a tenant cannot bring a ReDoS), with UI fields.
- **`GET /v1/usage`**: a tenant's own spend, remaining budget, requests in the last minute.
- **CSV cost export** (`/api/export/calls.csv`, button on Observability): per-call cost and latency, no prompt text, spreadsheet-formula-safe.
