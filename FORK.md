# lovestaco/linkedin-mcp-server — msgkit fork

Fork of [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server)
(Apache-2.0) used by msgkit-server's lead-enrichment workers, which start it over
stdio and call its tools directly (no LLM).

Changes live on `main`. **msgkit-server workers follow `main`**: each laptop checks main's
head commit every 15 minutes and restarts its browser on it between leads, so a push here
reaches every laptop within about 15 minutes. Run the tests before pushing. Tags
(`v<upstream>-msgkit.<n>`) are only the fallback compiled into msgkit-server
(`ForkTag` in `internal/limcp/client.go`) for a laptop that cannot reach GitHub.

Changes:

- `linkedin_mcp_server/tools/msgkit.py` — tools that open exactly one page each
  and return small structured results:
  - `msgkit_person_activity`: `/in/<user>/recent-activity/all/` only (no main profile
    load); newest posts with text, repost link, and each post's share link read through
    its "Copy link to post" menu item (the page's clipboard call is intercepted, the
    machine clipboard is never touched).
  - `msgkit_person_company`: `/in/<user>/details/experience/` for the current company link.
  - `msgkit_company_people`: `/company/<slug>/people/?facetCurrentFunction=8` (Engineering);
    waits for the insights to render and returns the page text, the company's numeric id
    and `ready`. The filter makes the count the same for every viewer (the "People
    highlights" line is personalised).
  - `msgkit_account`: the logged-in member's name, profile URL and Premium status.
- `server.py` registers them; `tests/fixtures/tool-contract/tools.json` records them.

Upstream is otherwise untouched; to take a new upstream release, merge `upstream/main` into
`main`, run the tests and tag again.

## Staying current with upstream

msgkit-server keeps this fork level with upstream automatically (its `internal/forksync`): the
sync laptop merges `upstream/main`, a Claude Code session resolves conflicts and adapts
`tools/msgkit.py`, the result is verified, tested in a real browser and pushed here. Status and
history are on msgkit-server's LinkedIn › Settings page. If it fails, nothing is pushed and the
app shows a red banner until someone merges by hand.
