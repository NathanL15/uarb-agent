# Keeping the agent up without a machine of your own

The inbox is the single source of truth for what has been answered: a handled
message is moved to the `UARB-Agent-Done` folder (IMAP) or labelled
`uarb-agent-done` (AgentMail). Any number of workers can therefore watch the
same inbox without a shared database, and none of them re-answers anything.

## Primary: GitHub Actions (`.github/workflows/poll.yml`)

- **Instant path (optional).** GitHub's `repository_dispatch` endpoint rejects
  AgentMail's webhook body, so a direct hook does not work; `deploy/relay/` has
  a Cloudflare Worker that forwards a clean dispatch, and
  `deploy/agentmail_webhook.py` points the inbox at it. With it, replies come
  in about 90 seconds instead of within the 5-minute schedule.
- **Schedule.** The workflow runs every 5 minutes, GitHub's minimum. Actions
  minutes are unlimited on a public repository; on a private one this cadence
  would exceed the 2,000 free minutes, so use every 30 minutes there.
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
