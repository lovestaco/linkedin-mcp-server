# lovestaco/linkedin-mcp-server — msgkit fork

Fork of [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server)
(Apache-2.0) used by msgkit-server's lead-enrichment workers, which start it over
stdio and call its tools directly (no LLM).

Changes (branch `msgkit`, tags `v<upstream>-msgkit.<n>`):

- `linkedin_mcp_server/tools/msgkit.py` — three tools that open exactly one page each
  and return small structured results:
  - `msgkit_person_activity`: `/in/<user>/recent-activity/all/` only (no main profile
    load); newest posts with text, repost link, and each post's share link read through
    its "Copy link to post" menu item (the page's clipboard call is intercepted, the
    machine clipboard is never touched).
  - `msgkit_person_company`: `/in/<user>/details/experience/` for the current company link.
  - `msgkit_account`: the logged-in member's name, profile URL and Premium status.
- `server.py` registers them; `tests/fixtures/tool-contract/tools.json` records them.

Upstream is otherwise untouched; rebase `msgkit` on new upstream releases and tag again.
