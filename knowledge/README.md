# knowledge/ — Reference Documentation

Full detail. [README.md](../README.md) is the overview and
[AGENTS.md](../AGENTS.md) is the guardrails; these are the documents they link to.

| Document | Read it when |
|---|---|
| [01_philosophy.md](01_philosophy.md) | Deciding whether to add a check, a test, or a comment |
| [03_writing_strategy.md](03_writing_strategy.md) | Writing a strategy: the `Variant` contract, config vs params |
| [04_pisces.md](04_pisces.md) | Cross-exchange arbitrage: startup, rebalance, shutdown |
| [05_corvus.md](05_corvus.md) | Single-shot order execution — the order lifecycle alone |

One document per subject, numbered in reading order. 04 and 05 are the two worked
strategies `qate` ships; read 05 first, it is the smaller.

02 was the env system — the run directory, the lock, config discovery — and moved
out with `qate.env` and `qate.boot` to
[qate-env](https://github.com/kamakuraquantlab/qate-env), where it is
`knowledge/01_env.md`. The number is left as a gap rather than reused, so a link
from outside this repository does not land on a different subject.

## Conventions

Three layers, and the top two are navigation rather than content:

| Layer | Files | Purpose |
|---|---|---|
| Human overview | `README.md` | What this is, who uses it, where to go |
| Agent entry point | `AGENTS.md` (`CLAUDE.md` points at it) | Guardrails, and the facts that cost you if missed |
| Technical reference | `knowledge/` | Full detail: contracts, config, design, workflow |

Entry points link; they do not duplicate. A fact that belongs in two places belongs
in one of them with a link from the other, because the copy is the one that goes
stale.

Reference documents number their headings (`## 1`, `### 1.1`) so that a cross
reference can point at a section and stay pointing at it. Entry points do not.
