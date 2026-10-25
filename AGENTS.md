# agents-webhook

Component `agents-webhook` (python) of the agents-webhook-component project. Its own tests, `MEMORY.md` and `ROADMAP.md` live here.

Run `mise run test`, `mise run lint` and `mise run fmt` from this directory.

Highest-risk surface: the public ingest (`POST /webhook/{suffix}`, no SSO) with its basic auth, and prompt framing (untrusted payload text becomes an agent prompt). Changes there need tests for the identical 401, the body cap and the escaped `<event>` block.
