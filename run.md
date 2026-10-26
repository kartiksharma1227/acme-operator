# How to run Acme Operator (step by step)

This guide runs the AI employee on your own machine so you can watch it work and record a demo.
You need **two terminals** and **a browser**.

- **Terminal A** runs the fake company ERP (the "Acme ERP" sandbox).
- **Terminal B** runs the AI employee.
- **The browser** lets you look at the ERP yourself, and shows the evidence report at the end.

---

## 1. One-time setup

Skip this section if `.venv/` already exists.

```bash
cd ~/Desktop/CentrAlign_AI
make setup      # creates .venv, installs Python packages and Chromium, copies .env.example to .env
```

You need Python 3.12. `uv` is used if it is installed, otherwise `pip`.

---

## 2. Choose the model (in `.env`)

The agent talks to an OpenAI model. You can reach it in one of two ways. Use **only one**.

### Option A: the local proxy (uses your ChatGPT Go quota, no API bill)

1. Open the **EasyCLIProxyAPI** app and make sure the proxy is started (it listens on port 8317).
2. `.env` should contain:
   ```
   LLM_BASE_URL=http://127.0.0.1:8317/v1
   LLM_API_KEY=<the proxy's client key>
   AGENT_MODEL=gpt-5.6-terra
   VERIFIER_MODEL=gpt-5.6-terra
   ```
3. Check that the proxy is up:
   ```bash
   curl -s http://127.0.0.1:8317/v1/models
   ```
   `{"error":"Missing API key"}` is fine: it means the proxy is running, and this curl just didn't
   send the key. Connection refused, or no output at all, means the proxy app is not running.

### Option B: the official OpenAI API (costs money, no quota limits)

1. In `.env`, put `#` in front of `LLM_BASE_URL` and `LLM_API_KEY`.
2. Make sure `OPENAI_API_KEY` is set.
3. Set the models, for example:
   ```
   AGENT_MODEL=gpt-5.4-mini
   VERIFIER_MODEL=gpt-5.4-mini
   ```
   Or use `gpt-5.5` for the best quality.

### Other useful `.env` settings

| Setting | Meaning |
|---|---|
| `HEADLESS=0` | Show the browser window while the agent works. Keep this at 0 for the demo. |
| `MAX_COST_USD=3.00` | The run stops if its estimated cost passes this. This only counts when the `PRICE_*` values are set. |
| `ACME_ERP_URL=http://localhost:8000` | Where the sandbox ERP runs. |
| `ACME_ERP_USER` / `ACME_ERP_PASSWORD` | The ERP login: `agent` / `acme-demo-123`. | 

---

## 3. Start the company ERP (Terminal A)

```bash
cd ~/Desktop/CentrAlign_AI
make reset            # fresh database: only the seed data, no bills or employees from earlier runs
make sandbox-chaos    # start the ERP with chaos ON
```

- Chaos mode renames buttons, expires sessions, returns random server errors and slows pages
  down. The agent has to recover from all of it, which is good to show in the demo.
- For a calm run without chaos, use `make sandbox` instead of `make sandbox-chaos`.
- **Leave this terminal running.** Stop it with `Ctrl+C`.

---

## 4. Look at the ERP yourself (browser)

1. Open **http://localhost:8000**.
2. Log in as **agent** / **acme-demo-123**.
3. Look around: **Purchase Orders**, **Bills**, **Exceptions**, **Employees**, **Equipment**,
   **Access Requests**.
4. **http://localhost:8000/admin/chaos** shows which chaos modes are on.

The company's "handbook" (what the agent reads to learn how Acme works) is in `company/`:
- `company/sops/invoice_processing.md` and `company/sops/employee_onboarding.md`: the procedures.
- `company/policies.yaml`: the rules, for example FIN-3 (bills over $10,000 need approval) and
  the AP-1 / AP-2 pre-submit checks.
- `company/systems.yaml`: which systems exist, and which form submissions count as real writes.
- `company/inbox/`: the vendor invoice PDFs.

---

## 5. Task 1: process the vendor invoices (Terminal B)

```bash
cd ~/Desktop/CentrAlign_AI
source .venv/bin/activate
make demo-invoices
```

This is the same as `python -m agent run "Process the vendor invoices in the inbox."`.

**What you will see:**
1. **The contract panel:** the agent writes down what "done" means (acceptance criteria) before
   it touches anything.
2. **The plan checklist.**
3. **A Chromium window opens** and the agent works the ERP by itself: it reads the PDFs, searches
   bills and opens purchase orders.
4. **Yellow recovery lines** when chaos hits: a renamed button found again, a re-login after a
   session expiry, a retried server error.
5. **An approval prompt** for the **$12,800 Initech invoice** (rule FIN-3). The terminal asks
   `Approve? [y/n]`. Type `y`, then press Enter at "Note (optional)" or type a short note.
6. The **duplicate invoice INV-0987 is skipped**, and two invoices go to **exceptions** (an amount
   mismatch, and a PO that doesn't exist).
7. **Verification:** a second model with fresh context and read-only tools checks every claim
   against the live ERP.
8. At the end, the terminal prints the path to the **evidence report** (`report.html`).

**The correct end result** (you can check it in the ERP's Bills and Exceptions pages):

| Invoice | Outcome |
|---|---|
| INV-1001 (Globex, $2,450) | Bill created |
| INV-1002 (Initech, $12,800) | Bill created after FIN-3 approval |
| INV-1005 (Globex, $1,203.50) | Bill created |
| INV-1003 (Umbrella) | Exception: amount mismatch |
| INV-1004 (Stark) | Exception: PO not found |
| INV-0987 | Skipped: already billed (duplicate) |

A run takes about **5 to 8 minutes** with chaos on.

---

## 6. Open the evidence report

```bash
python -m agent runs                     # lists recent runs with their IDs
python -m agent report <run-id> --open   # opens report.html in your browser
```

Each run's files are in `runs/<run-id>/`: `report.html`, `events.jsonl` (everything that
happened), `state.json` and the screenshots.

The report shows:
- what the agent understood
- the results, with bill and exception numbers
- the verification table, with screenshots
- the approval: who, when, and the values
- the reliability log: retries, re-logins and re-located elements

---

## 7. Optional: catch a lie (re-verification)

Delete a bill behind the agent's back, then ask it to verify again:

```bash
sqlite3 sandbox/acme.db "DELETE FROM bills WHERE bill_number='BILL-0004'"
python -m agent verify <run-id>
```

The verifier should now fail the criterion about that bill. This shows it checks the real system,
not the agent's word.

---

## 8. Task 2: onboard an employee (same code, different job)

First reset the ERP. In **Terminal A** press `Ctrl+C`, then run:
```bash
make reset && make sandbox-chaos
```

In **Terminal B**:
```bash
make demo-onboarding
```

This is the request "Onboard Priya Sharma — she's joining Sales as an Account Executive this
coming Monday, reporting to Daniel Ruiz."

**What to point out:**
- "This coming Monday" is turned into a real date. The date is relative to the day you run it:
  run on Monday 2026-10-26, it becomes 2026-11-02.
- It **skips laptop MBP-0011**, because that one is in repair, and assigns an available
  MacBook Pro 14.
- It **stops for the IT-7 approval** (equipment assignment). Type `y`.
- It requests **Email and CRM access at standard level only**. It never requests the Finance Portal.

Nothing in `agent/` knows about invoices or onboarding. You can prove it with:
```bash
grep -ril "invoice" agent/      # prints nothing
```

---

## 9. Optional: a vague request, answered later (durable human-in-the-loop)

Reset first (`Ctrl+C` in Terminal A, then `make reset && make sandbox-chaos`). Then:

```bash
python -m agent run "Onboard Priya Sharma" --human file
```

The agent asks for the missing details **before creating anything**, saves the question, and
exits. Answer it whenever you like, even from a new terminal:

```bash
python -m agent resume <run-id> --answer "Sales, Account Executive, starting this coming Monday, reporting to Daniel Ruiz"
```

It continues, then pauses again for the IT-7 approval:

```bash
python -m agent resume <run-id> --approve
```

You can also use `--deny`. Either one can take `--note "..."`.

---

## 10. Optional: a task with no procedure

Reset first, then:

```bash
python -m agent run "Export all open purchase orders above \$5,000 to a CSV in the outbox." --human file
python -m agent resume <run-id> --approve     # there is no SOP, so it asks you to approve its plan first
```

The CSV appears in `company/outbox/`.

---

## 11. Tests and evals

```bash
make test           # 143 tests. Free: no model calls.
make evals-quick    # 2 scenarios (clean invoices + clean onboarding). Uses model quota.
make evals          # all 7 scenarios. Uses a lot of quota, about 30 to 40 minutes.
```

Results are written to `evals/results.md`. Evals start their own sandboxes on free ports, so you
don't need Terminal A for them.

---

## 12. All commands

| Command | What it does |
|---|---|
| `make setup` | One-time install |
| `make reset` | Put the ERP database back to the seed data |
| `make sandbox` / `make sandbox-chaos` | Start the ERP on :8000, with chaos off or on |
| `make demo-invoices` | Run the invoice task |
| `make demo-onboarding` | Run the onboarding task |
| `python -m agent run "<request>"` | Run any request. Flags: `--headless`, `--human terminal\|file\|auto-approve\|auto-deny`, `--max-steps N` |
| `python -m agent resume <id> --approve\|--deny\|--answer "..."` | Continue a paused run |
| `python -m agent verify <id>` | Re-verify a finished run against the ERP as it is now |
| `python -m agent report <id> --open` | Rebuild and open the evidence report |
| `python -m agent runs` | List recent runs |
| `make test` / `make evals-quick` / `make evals` | Tests and evals |

---

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| `command not found: python` or import errors | Run `source .venv/bin/activate` in that terminal first. |
| `Address already in use` when starting the sandbox | Something is already on port 8000. Stop the old sandbox (`Ctrl+C`), or run `lsof -iTCP:8000` to find it. |
| The agent can't reach the ERP | Terminal A isn't running. Start it with `make sandbox-chaos`. |
| `APIConnectionError` | The proxy app isn't running (Option A). Start EasyCLIProxyAPI. |
| `RateLimitError` / `usage_limit_reached` | Your ChatGPT Go quota is used up. Switch to a second account in the proxy app, or to Option B. |
| `InternalServerError` with an HTML "Gateway Timeout" page that mentions `127.0.0.1` | Your network (office or college Wi-Fi) has a system proxy, and Python sent the local request through it. Make sure `.env` has `NO_PROXY=localhost,127.0.0.1,::1`. |
| `Invalid API key` | With the proxy, `LLM_API_KEY` must be the proxy's own client key, not your OpenAI key. |
| Old bills or employees left from a previous run | Stop the sandbox, then run `make reset` and start the sandbox again. |
| The browser window doesn't appear | Set `HEADLESS=0` in `.env`, and don't pass `--headless`. |

---

## 14. Recording checklist

1. Do **one full dry run** first.
2. Then stop the sandbox, run `make reset`, and start it again, so the ERP is clean on camera.
3. Make the terminal font bigger. About 110 columns wide works well.
4. Keep a good earlier report open in a browser tab as a backup.
5. Follow `demo.md` for the full recording guide and what to say in each scene.
6. Record the whole run, then speed up the slow parts while editing.
