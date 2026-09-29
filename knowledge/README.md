# knowledge/ — Reference Documentation

Full detail. [README.md](../README.md) is the overview and
[AGENTS.md](../AGENTS.md) is the guardrails; these are the documents they link to.

| Document | Read it when |
|---|---|
| [philosophy.md](philosophy.md) | Deciding whether to add a check, a test, or a comment |
| [strategy_layout.md](strategy_layout.md) | Writing a strategy: the `Variant` contract, config vs params |
| [strategies.md](strategies.md) | Reading the two worked strategies `qate` ships |
| [pisces.md](pisces.md) | Why pisces is built the way it is — startup, rebalance, shutdown |
| [env.md](env.md) | Environments: the directory, the lock, logging, config discovery |

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
