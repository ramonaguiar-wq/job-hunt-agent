# Job Hunt Agent

A personal agent that hunts design jobs, ranks them against hard rules, and helps with LinkedIn outreach, with a human approving every outgoing message.

Built for my own search as a remote Product Designer. It runs hourly, reads public job sources, and never applies or messages anyone on its own.

## What it does

```
 public job sources ──► monitor/ (Node) ──► filters + rank ──► 14-day pool (engage-jobs.json)
 RemoteOK, Remotive,     collect, dedupe,    hard rules in        │
 Himalayas, Dribbble,    fetch full text     job-criteria.json    ▼
 WWR, Greenhouse/Lever/                                      agent/ (Python, local web panel)
 Ashby, LinkedIn (public)                                    review jobs, reject with a reason,
                                                             draft posts/comments via Claude CLI,
                                                             approve each one before it goes out
```

- **Hard rules as data.** Seniority, remote policy, countries, language, currency and contract type live in `monitor/job-criteria.json`. A JS engine (`filters.js`) and a Python mirror (`criteria.py`) apply them, and `test-criteria.js` proves both give identical results on frozen real-world cases.
- **Learns from rejections.** Each job you reject in the panel records a reason. Repeated patterns become *suggested* rules; nothing is added without approval.
- **Human in the loop.** The agent drafts; you approve, edit or reject. No auto-apply, no auto-send.
- **No account scraping.** LinkedIn jobs come from public pages only, with pauses and a block detector.

## Layout

| Path | Role |
|---|---|
| `monitor/` | Collector, ATS fetchers, filters, ranking, notifications |
| `agent/` | Local panel (`localhost:8765`), drafting, engagement queue, application tracker |
| `agent/prompts/` | Templates for your profile, strategy and style (fill them in) |

## Run

Requires Node 18+ and Python **3.12+** (the code uses f-string syntax that 3.9 rejects). Drafting uses the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude -p`), already logged in.

```bash
cp .env.example monitor/.env
cd monitor && npm install
npm test                 # JS/Python rule parity
node job-monitor.js      # one collection run
cd ../agent && python3 agent.py   # panel on http://localhost:8765
```

Edit `agent/prompts/*.md` and `agent/config.json` first. The shipped `job-criteria.json` is my own ruleset, kept as a worked example, so change it to yours.

## Status

Personal tool, published as a case study. Comments are in Portuguese and English. Scheduling (launchd) is not included.

MIT license.
