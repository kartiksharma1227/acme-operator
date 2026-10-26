# Eval results

_Two sessions on the same model and commit, merged: the invoice scenarios ran first; the plan's usage quota ran out during the deny run's verification, so the other four ran on a second session. A run stopped by the model provider is never counted against the agent._

2026-10-25 13:23 · model `gpt-5.6-terra (via local proxy; executor and verifier)` · commit `ffbaa1b` · chaos seed 42

**Ground truth: 7/7 scenarios correct · verifier agreed with ground truth 5/6 · false passes: 0**

Ground truth is read straight from the sandbox database and the outbox, independently of the agent and of its verifier. *Agrees* means the verifier's verdict (pass vs not pass) matched it; a *false pass* (verifier passed, ground truth failed) is the dangerous kind of disagreement.

| scenario | ground truth | run status | verifier said | agrees? | steps | recoveries | repairs | human | tokens in/out | cost | time |
|---|---|---|---|---|---|---|---|---|---|---|---|
| invoices_clean | ✅ 14/14 | completed | pass | ✅ | 64 | 0 | 0 | 1 | 1.07M / 11k | — | 7m44s |
| invoices_chaos | ✅ 14/14 | completed | pass | ✅ | 74 | 12 | 0 | 1 | 1.40M / 13k | — | 12m25s |
| invoices_deny | ✅ 13/13 | failed | none | — | 66 | 0 | 0 | 1 | 1.10M / 9k | — | 7m54s |
| onboarding_clean | ✅ 12/12 | completed | pass | ✅ | 34 | 0 | 0 | 1 | 0.37M / 6k | — | 4m15s |
| onboarding_chaos | ✅ 12/12 | partial | partial | ❌ | 37 | 4 | 0 | 1 | 0.41M / 7k | — | 5m45s |
| onboarding_ambiguous | ✅ 13/13 | completed | pass | ✅ | 36 | 0 | 0 | 2 | 0.41M / 8k | — | 5m40s |
| po_export_no_sop | ✅ 5/5 | completed | pass | ✅ | 17 | 0 | 0 | 1 | 0.16M / 5k | — | 3m02s |

## Failures and findings

### invoices_deny
- ⚠️ stopped by the model provider: `RateLimitError: Error code: 429 - {'error': {'type': 'usage_limit_reached', 'message': 'The usage limit has been reached', 'plan_type': 'go', 'resets_at': 17955`
- report: `runs/evals/20261025-122151/invoices_deny/runs/20261025-124202-f22a/report.html`

### onboarding_chaos
- verifier disagreed with ground truth (false alarm: verifier said `partial`)
- why: the contract phrased a criterion about the past ("the laptop *was available* for assignment"); the ERP shows
  only the current state, so the verifier called it unverifiable rather than vouch for it. The work itself was correct.
- report: `runs/evals/20261025-130433/onboarding_chaos/runs/20261025-130850-42f6/report.html`


_Run folders (`runs/…`) are local artifacts and are not committed._
