# kit-campus-mcp

An MCP server for the KIT Campus portal (`campus.studium.kit.edu`). It reads
exam results, exam registrations, study progress, your timetable and the public
module catalogue, and it can report which results are **new since the last
check** - which is what a notification bot needs.

## How it works

`campus.studium.kit.edu` is only a KIT-branded frame. The application behind it
is CAS Campus at `https://campus.kit.edu/sp`, and the frame's own
`kit-frame-config.js` maps every portal page to a backend URL. This server talks
to those backend URLs directly.

Two access levels:

| | Login | Pages |
|---|---|---|
| **Public** | none | module catalogue, course catalogue, degree programmes |
| **Account** | Shibboleth SSO + token | results, exam registrations, study progress, timetable |

The account side is the interesting part. `campus.kit.edu`'s own login page
(`campus/login/chooseCampusUser.asp`) no longer exists on KIT's server - it
returns a 404 - so logging in there directly is impossible. What the portal
actually does:

1. authenticate against **the portal's** SP:
   `campus.studium.kit.edu/Shibboleth.sso/Login` -> `idp.scc.kit.edu` -> SAML POST back
2. call `campus.studium.kit.edu/token.php`, which returns a short-lived
   `tokenA` plus the account's name and matriculation number
3. pass that token to the backend as `login-token` + `login-ts` on any
   `campus.kit.edu` URL, which is what creates the CAS Campus session

This server does the same three steps. Cookies and the token are cached, so
repeated polls do not re-run the SSO chain.

## Install

```bash
cd C:\Users\blade\kit-campus-mcp && uv venv && uv pip install -e .
```

Copy `.env.example` to `.env` and fill in your KIT account:

```
KIT_USERNAME=ab1234
KIT_PASSWORD=...
```

`KIT_USERNAME` is the short KIT account (`ab1234`), not your e-mail address.

Verify:

```bash
.venv\Scripts\kit-campus login
```

That prints who you are, your degree programmes with their GUIDs, and the
current term. Any tool that takes a `program_guid` accepts those; omit it and
the first one is used.

## Register with an MCP client

```bash
claude mcp add kit-campus -- C:\Users\blade\kit-campus-mcp\.venv\Scripts\kit-campus-mcp.exe
```

Or in a client config file:

```json
{
  "mcpServers": {
    "kit-campus": {
      "command": "C:\\Users\\blade\\kit-campus-mcp\\.venv\\Scripts\\kit-campus-mcp.exe",
      "env": { "KIT_USERNAME": "ab1234", "KIT_PASSWORD": "..." }
    }
  }
}
```

## Tools

**Public catalogue - no login**

| Tool | Purpose |
|---|---|
| `kit_search_modules` | Search the Modulhandbuch by title, id, code or ECTS range |
| `kit_get_module` | One module in full: lectures, **exam dates with rooms**, Teilleistungen, programmes |
| `kit_search_events` | Search the Vorlesungsverzeichnis |
| `kit_list_programs` | List degree programmes |

**Your account - needs credentials**

| Tool | Purpose |
|---|---|
| `kit_check_session` | Verify login, show who you are, list your programme GUIDs |
| `kit_get_grades` | Every result with grade, ECTS, date, plus the official average |
| `kit_check_new_results` | **Only what changed since the last check** - poll this from a bot |
| `kit_list_registered_exams` | Exams you are registered for, with dates, rooms and deadlines |
| `kit_get_study_progress` | Full study tree: what is done, what is still open |
| `kit_get_timetable` | Personal timetable |
| `kit_fetch_page` | Any other Campus page, returned as parsed tables |

Results come from the study tree on `contractview.asp`, whose `brick`
(Teilleistung) rows are the actual exam results - `M-…` modules and the sections
above them are aggregates. A grade of `be` means *bestanden* (passed, ungraded).
A Teilleistung counted in two sections appears twice in the raw page; the client
de-duplicates it so totals and notifications stay correct.

Everything is read-only against KIT. Nothing registers or de-registers you from
an exam; `kit_check_new_results` writes only its local snapshot file.

## Telegram bot

`examples/telegram_bot.py` polls for new results and messages you when one
appears. It needs no extra dependencies - it calls the Telegram Bot API over the
`httpx` this package already uses. Put the two values in `.env`, then:

```bash
.venv\Scripts\python examples\telegram_bot.py --interval 1800
```

It also answers commands in the chat: `/noten`, `/pruefungen`, `/status`,
`/modul <id>`.

To get the two values: message `@BotFather` to create a bot and copy the token,
then message your own bot once and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` to read your chat id.

The first run stores a baseline and stays quiet, so you do not get a message for
every result you already have. After that, only new or changed results are sent.

Run it as a scheduled task to keep it alive across reboots:

```powershell
schtasks /create /tn "KIT Noten" /tr "C:\Users\blade\kit-campus-mcp\.venv\Scripts\python.exe C:\Users\blade\kit-campus-mcp\examples\telegram_bot.py --once" /sc hourly
```

## CLI

```bash
kit-campus login                     # check credentials, list programmes
kit-campus grades                    # print the Notenspiegel
kit-campus check                     # print what changed since last time
kit-campus check --dry-run           # ... without consuming the change
kit-campus modules "Regelungstechnik"
kit-campus module M-ETIT-101156      # module detail incl. exam dates
kit-campus dump student/timetable.asp
kit-campus reset --watch grades      # forget the baseline
```

## When a page does not parse

The parsers key off `<table class="listview">`, the one layout CAS Campus uses
everywhere, and map German column headers onto canonical field names. If KIT
changes a page, dump it and look at what came out:

```bash
kit-campus dump student/contractview.asp
```

That writes the raw HTML and a `.tables.json` with every table, its id, its
headers and its rows. Adding the new header label to `HEADER_ALIASES` in
`parsers.py` is usually the whole fix.

## Notes and limits

- **Every tool is verified against the live site with a real account**, public
  and account-side alike.
- **ARM64 Windows**: `cryptography` (pulled in by `mcp`) ships no `win_arm64`
  wheel after 46.0.3, so `pyproject.toml` pins it on that platform. Without the
  pin, installing tries to compile it and fails unless Rust is present.
- **Two-factor.** If your KIT account is enforced to MFA, scripted password
  login cannot complete it. The error message says so explicitly and names the
  step it stopped at. Workaround: log in with a browser and copy the
  `_shibsession_*` cookie into the cookie file printed by `kit-campus login`.
- **Rate.** Poll every 15-30 minutes at most. Results are published in batches,
  not continuously, and campus.kit.edu has maintenance windows at night.
- Credentials live in `.env` (gitignored) or the environment; cookies and
  snapshots live in `%LOCALAPPDATA%\kit-campus-mcp`.
