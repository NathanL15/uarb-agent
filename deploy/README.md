# Keeping the agent up

The inbox is the single source of truth for what has been answered: a handled
message is moved to the `UARB-Agent-Done` folder (IMAP) or labelled
`uarb-agent-done` (AgentMail). Any number of workers can therefore watch the
same inbox; the only rule is that backups wait a few minutes before touching a
message, so the primary gets first go.

## Layer 1: the process looks after itself

`uarb-agent run` recycles its browser after two consecutive failures, retries a
failed send three times, writes `data/heartbeat.json` on every poll, holds a
power request on Windows so the laptop does not sleep, and exits with code 3
when `.env` changes so a supervisor restarts it with the new settings.
`uarb-agent status` reads the heartbeat and exits non-zero if it is stale.

## Layer 2: a supervisor on the machine

Windows: `powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1`
registers a Scheduled Task that starts `deploy\windows\run_agent.cmd` at logon and
re-launches it every 5 minutes if it is not running. The .cmd loops forever
around `uarb-agent run`. Logs go to `logs\`. Remove with `-Uninstall`.

Linux VM: `deploy/vm/cloud-init.yaml` builds the Docker image and installs a
systemd unit with `Restart=always`. Paste the `.env` contents into the file
before using it as the instance's user data (GCP: "Automation / Startup script"
takes cloud-init on Ubuntu images; Oracle: "Cloud-init script").

## Layer 3: off the machine entirely

`.github/workflows/backup-poll.yml` runs every 30 minutes on GitHub's runners
and answers anything that has waited more than 3 minutes. Needs the mailbox
values as repository secrets (`gh secret set MAIL_USER` etc.). Idle runs cost
under a minute, so it fits in a private repo's free minutes.

## Checking on it

```
uarb-agent status            # heartbeat age, handled count, failures
uarb-agent pending           # what is waiting in the inbox right now
type logs\agent.log          # Windows
journalctl -u uarb-agent -f  # VM
```
