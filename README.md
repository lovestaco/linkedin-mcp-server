# LinkedIn MCP Server (lovestaco fork)

<!-- mcp-name: io.github.stickerdaniel/linkedin-mcp-server -->

A fork of [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server)
(Apache-2.0) that drives your own logged-in LinkedIn browser session. Everything upstream offers is
still here; this fork adds a small set of **one-page tools for bulk lead research**, built to cost as
few LinkedIn page views as possible and to look like a person reading the page.

For general setup, login and the upstream tools, see the upstream project. This page covers only
what the fork adds.

## What the fork adds

All of it lives in [`linkedin_mcp_server/tools/msgkit.py`](linkedin_mcp_server/tools/msgkit.py) and is
registered in `server.py`. Each tool opens **at most one** LinkedIn page and returns small,
structured results (no screenshots, no free-form page dumps).

| Tool | Opens | Returns |
|---|---|---|
| `msgkit_person_activity` | `/in/<user>/recent-activity/all/` (never the main profile) | the newest posts: text, whether it is a repost, the original post link, and each post's share link |
| `msgkit_person_company` | `/in/<user>/details/experience/` | the experience entries in page order; the one marked "Present" is the current job, with its numeric company link |
| `msgkit_company_people` | `/company/<slug>/people/?facetCurrentFunction=8` (Engineering) | the page text, the company's numeric id and a `ready` flag |
| `msgkit_account` | `/in/me/` | the logged-in member's name, profile URL and whether the account is Premium |
| `msgkit_connect` | `/in/<user>/` (one view) | sends one connection request (no note unless given) and returns the profile fields read off that page: followers, place, country, company, company position, headline |
| `msgkit_list_connections` | `/mynetwork/invite-connect/connections/` (one page load, then scrolling) | the account's connections newest first (name, link, tagline, connected-on, message link), stopping at a given profile, at the end, or after three scrolls that load nothing; an empty page is reported as `empty`, never as up to date |
| `msgkit_idle` | nothing (stays on the open page) | spends N seconds like a person reading: small uneven scrolls, mouse drifts, pauses |

### Details worth knowing

- **Posts cost one page view.** `msgkit_person_activity` skips the main profile page that
  upstream's person tool loads first. Each post's link is read through LinkedIn's own
  "Copy link to post" menu item: the page's clipboard call is intercepted in the page, and your
  machine's clipboard is never touched. The result is an `lnkd.in` short link; following it (a plain
  HTTP request, not a LinkedIn page view) gives the canonical post URL, whose id encodes the exact
  post time.
- **Engineering headcount is the same for every viewer.** The unfiltered People page lists only the
  top five job functions, and its "People highlights" line depends on who is looking. With the
  Engineering filter, "N associated members" and the first row of "What they do" are the Engineering
  headcount for everyone. The tool waits (up to 20 s, scrolling gently) until those insights have
  rendered; `ready: false` means they never did, so retry instead of trusting the text.
- **The current company comes from the experience page**, because the main profile header shows it
  as plain text without a link. Single-role entries read `Title` / `Company · Full-time`; multi-role
  entries read `Company` / `26 yrs 4 mos`.
- **Premium detection** (`msgkit_account`) looks for the absence of the "Try Premium" prompt together
  with Premium icons, and returns `null` when it cannot tell.
- **`msgkit_connect` costs one profile view.** It reads the page, opens the invite dialog from the
  profile's own Connect link, chooses "Send without a note" (or sends `note`), and checks the result on
  the same page instead of reloading. It reports `pending` / `already_connected` without sending when
  the person is already invited or connected, and `sent_unverified` when the dialog closed but the page
  still shows Connect (check the profile before sending again). `dry_run=true` reads everything and
  sends nothing. If LinkedIn does not open the dialog from the page, it uses the invite page and says
  so (`via: "deeplink"`, `extra_profile_view: true`).
- **`msgkit_idle` never clicks, types or navigates**, so it can be used freely between page loads
  without adding page views.

### Running it

```sh
uvx --from git+https://github.com/lovestaco/linkedin-mcp-server mcp-server-linkedin --login   # sign in once
uvx --from git+https://github.com/lovestaco/linkedin-mcp-server mcp-server-linkedin --transport stdio
```

Pin a commit or tag after `@` (for example `...linkedin-mcp-server@<commit>`) to run an exact version.
LinkedIn blocks headless Chromium on company People pages, so run the browser headed
(`HEADLESS=false`).

## Staying current with upstream

LinkedIn changes its pages often, and upstream fixes the scraper quickly, so this fork is kept level
with it: upstream's `main` is merged in, conflicts are resolved in favour of upstream's code with
the additions above kept, `tools/msgkit.py` is checked against any internals upstream changed, the
tests are run, and the result is pushed to `main`. To do it by hand:

```sh
git remote add upstream https://github.com/stickerdaniel/linkedin-mcp-server.git   # once
git fetch upstream && git merge upstream/main
# on conflicts in README.md keep this fork's version
uv run pytest -q tests/test_server.py
```

`tests/fixtures/tool-contract/tools.json` records every tool the server exposes; regenerate it when
the tool list changes.

## Credits and license

Built on the work of [Daniel Sticker](https://github.com/stickerdaniel/linkedin-mcp-server) and its
contributors. Licensed under the Apache License 2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)).
Independent open-source project; not affiliated with LinkedIn or Microsoft. LinkedIn is a trademark
of LinkedIn Corporation.

---

## Review every commit with LiveReview

This repo is committed through [LiveReview](https://hexmos.com/livereview/) (`git-lrc`), a git
pre-commit hook that reviews your staged changes with AI before they are committed.

```sh
# install lrc: https://github.com/HexmosTech/git-lrc
lrc setup           # one-time sign-in and AI connector
lrc hooks install   # global pre-commit hook for every repository
```

After that, `git commit` asks for a review of the staged diff first. To commit without a review
(this still records the attestation the hook checks for):

```sh
git add -A
lrc review --skip
git commit -m "..."
```

Turn the hook off for one repository with `lrc hooks disable` and back on with `lrc hooks enable`;
`lrc hooks status` shows where it stands.
