# Keeping the agent up without a machine of your own

The inbox is the single source of truth for what has been answered: a handled
message is moved to the `UARB-Agent-Done` folder (IMAP) or labelled
`uarb-agent-done` (AgentMail). Any number of workers can therefore watch the
same inbox without a shared database, and none of them re-answers anything.

## Primary: GitHub Actions (`.github/workflows/poll.yml`)

- **Instant path.** AgentMail calls GitHub's `repository_dispatch` endpoint the
  moment a message arrives (`deploy/agentmail_webhook.py` sets that up; it needs
  the AgentMail API key and a fine-grained GitHub token with Contents write on
  this repo). The workflow starts within seconds and answers in about two
  minutes: one minute of runner setup and Playwright install, then the fetch.
- **Safety net.** The same workflow also runs every 30 minutes, so a missed
  webhook or an IMAP inbox (which has no webhook) still gets answered. An idle
  run costs about one billed minute; 48 runs a day is roughly 1,450 minutes a
  month, inside a private repo's 2,000 free minutes.
- **Secrets.** `gh secret set MAIL_TRANSPORT`, `MAIL_ADDRESS`, and either
  `AGENTMAIL_API_KEY` + `AGENTMAIL_INBOX_ID` or `MAIL_USER` + `MAIL_PASSWORD`
  (+ `IMAP_HOST`, `SMTP_HOST` if not Gmail), plus `UARB_ATTACHMENT_MB` (4 for
  AgentMail, 18 for Gmail). Without secrets the workflow finds nothing and exits.
- **Audit trail.** Each run that answered something uploads its `runs/` folder
  as a workflow artifact for 30 days.

## Optional: a free VM for lower latency

`deploy/vm/cloud-init.yaml` builds the Docker image and installs a systemd unit
with `Restart=always` on a GCP e2-micro or Oracle always-free instance. Paste
the `.env` contents into the file first. With a VM in place, Actions becomes the
backup: change its `pending`/`run` steps to `--min-age 180` so the VM answers
first.

## Optional: a machine you control

`deploy/windows/install_task.ps1` registers a Scheduled Task that keeps
`uarb-agent run` alive on a Windows box (logon start, relaunch every 5 minutes,
wake to run, battery allowed). Remove with `-Uninstall`.

## Checking on it

```
gh run list --workflow "answer mail" --limit 10
uarb-agent pending           # what is waiting in the inbox right now
uarb-agent status            # heartbeat of a long-running worker (VM / Windows)
```
