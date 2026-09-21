# uarb-agent

An email agent for the Nova Scotia UARB [Public Documents Database](https://uarb.novascotia.ca/fmi/webd/UARB15).
Email it a matter number and a document type; it navigates the database, downloads
up to 10 of those documents, zips them, and replies with the ZIP, the file counts
for the matter and a short summary of the matter's metadata.

```
To:      <the agent's address>
Subject: M12205

Hi Agent, can you give me the Other Documents from M12205? Thanks!
```

```
Subject: M12205 Other Documents

Hi,

M12205 is about the Halifax Regional Water Commission - Windsor Street Exchange
Redevelopment Project - $69,275,000. It relates to Capital Expenditure Approvals
within the Water category. Its status is Open. The matter had an initial filing on
April 7, 2025 and a decision on October 23, 2025. I found 13 Exhibits, 6 Key
Documents, and 43 Other Documents and no Transcripts or Recordings, 62 files in
total. I downloaded 10 out of the 43 Other Documents and am attaching them as a
ZIP here.

Attached:
  - 102674  Board Order (07/08/2026)
  - 102454  HRWC (Board) Letter - Reply Submission (06/22/2026)
  ...
```

## How it works

```
 email in ──> request_parser ──> uarb (Playwright) ──> bundler ──> reply ──> email out
              rules, then a      open matter,          zip, split   templated
              local LLM only     read header + tab     by size      from the
              when rules can't   counts, scroll the    budget       scraped
              decide             grid, GO GET IT                    facts
```

- **`uarb_agent/uarb.py`** drives the FileMaker WebDirect site in headless Chromium.
  The site has no API, and its element ids change with the window size, so the
  client reads the page by *labels and value shapes*: header fields are matched to
  the label sitting above them, document rows are classified by what each cell
  looks like (doc number, exhibit code, date, extension, security), and per-tab
  counts are read from the tab labels. The document grid is virtualised, so the
  client scrolls it and waits for each row's data to arrive before reading it.
- **`uarb_agent/request_parser.py`** turns the email into `(matter, doc types, limit)`.
  Rules handle the normal cases deterministically. If they cannot decide, an
  optional local LLM (Ollama) is asked, and its answer is checked against the
  email: it cannot name a matter that is not written there.
- **`uarb_agent/bundler.py`** zips the downloads and splits into parts when they
  exceed the mail transport's attachment ceiling. A single file larger than the
  ceiling is reported in the reply instead of silently dropped.
- **`uarb_agent/reply.py`** writes the reply from the scraped fields. Nothing in the
  reply is generated, so the numbers always match the site.
- **`uarb_agent/mail/`** has three transports: a directory mailbox for local use and
  tests, IMAP/SMTP (Gmail app password or any provider), and AgentMail's REST API.
- **`uarb_agent/agent.py`** is the loop. Every request leaves a run folder under
  `data/runs/` with the parsed request, each step with a timestamp, the listing,
  what was downloaded or skipped, the ZIP and the reply text.

## Running it

```bash
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env            # pick a MAIL_TRANSPORT and fill in credentials

uarb-agent fetch M12205 "Other Documents"      # just the fetch, prints the metadata and the zip path
uarb-agent ask "Other Documents from M12205 please"   # full pipeline through the local mailbox, prints the reply
uarb-agent run                                 # poll the configured mailbox and answer requests
uarb-agent parse "audio for M10431"            # how a request would be read
```

With `LLM_BACKEND=ollama` and a local Ollama model, `ask`, `run` and `parse` use the
LLM fallback for phrasing the rules cannot read.

`docker build -t uarb-agent . && docker run --env-file .env -v uarb-data:/data uarb-agent`
runs the loop in a container with the Playwright browsers already installed.

## Tests and evals

```bash
pytest                              # offline: parser, row classifier, bundler, reply, transports, agent loop
python -m evals.parser_eval --llm   # 36 phrasings; rules alone vs rules + Ollama
python -m evals.live_eval           # against the live site; reports land in evals/reports/
```

Latest results are in `evals/reports/`: 56 offline tests; parser 31/31 on the cases
rules should handle and 35/36 with the Ollama fallback; live eval 12/12 cases and
253/253 checks against the site on 2026-09-21.

The live eval does not hard-code counts, because the database changes as filings
arrive. Each check compares the agent to the site itself: the number of rows read
from a tab must equal the count in the tab's label, downloads must equal
`min(limit, count)`, every file must have the right magic bytes and every ZIP must
open with the right member count and sit under the attachment budget. A few facts
that are years old (a title, a filing date) are pinned as a sanity check on the
field mapping. Two further cases check that an unknown matter gets a "not found"
reply and that the brief's example request produces a reply whose counts, title,
"downloaded X out of Y" and ZIP contents all agree.

## Keeping it running

`deploy/README.md` covers the three layers: the process heals itself (browser
recycling, send retries, heartbeat, keep-awake, restart on `.env` change), a
Windows Scheduled Task or a systemd unit on a VM restarts it after any crash or
reboot, and a GitHub Actions cron answers anything the primary has left waiting
for more than three minutes. Handled mail is marked in the mailbox itself, so
the workers never answer the same request twice. `uarb-agent status` shows the
heartbeat; `uarb-agent pending` shows what is waiting.

## Limits worth knowing

- One request is handled at a time; the WebDirect server does not like parallel sessions from one client.
- Hearing audio can be hundreds of MB. `UARB_MAX_TOTAL_MB` caps how much is pulled for one request and
  `UARB_ATTACHMENT_MB` caps each email; anything over the caps is named in the reply so nothing is lost silently.
- A request that names two matters gets a clarification rather than a guess.
