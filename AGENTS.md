# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## 5. Minimal Upstream Impact

**Non-negotiable: keep every Sycee-owned feature isolated by default and make any upstream integration minimal, explicit, and easy to reapply after an update.**

For all self-developed Sycee features:
- Put backend code under `backend/app/sycee/` and frontend code under `frontend/src/features/sycee/`.
- Put feature tests in `backend/tests/test_sycee_*.py` or inside the corresponding frontend Sycee feature directory.
- Register backend APIs only through `backend/app/sycee/router.py`.
- Register frontend routes and navigation only through `frontend/src/features/sycee/registry.tsx`.
- Use the exported frontend `request()` helper; do not add Sycee business types or methods to `frontend/src/lib/api.ts`.
- Reuse upstream APIs and public service boundaries. Keep Sycee business logic out of upstream-owned files.
- Store Sycee-owned data separately and reference upstream entities by stable IDs. Do not add Sycee fields to upstream data files or schemas.
- Never copy an upstream module in order to customize it.
- When a feature must appear inside an upstream UI, add only a thin adapter, slot, button, or registry call. The upstream file may pass context into Sycee code but must not implement Sycee behavior.
- Prefer one shared integration point over edits to several pages.
- Record every intentionally touched upstream file and its reason in `docs/sycee-integrations.json`.
- Do not integrate directly into upstream strategy, backtest, indicator, market-data, or storage internals. Redesign around their public outputs instead.

The existing gateway lines in `backend/app/main.py`, `frontend/src/router.tsx`, `frontend/src/lib/navRegistry.ts`, and `frontend/src/lib/api.ts` are the preferred stable integration points. Additional upstream touches are allowed only when they are necessary for a user-facing workflow, contain glue code only, and are added to the integration manifest.

This rule applies to Sycee feature development, not to commits whose sole purpose is merging a new upstream release. Before completing a Sycee feature, inspect its changed-file list. Every business-code change must stay inside Sycee-owned paths; every touched upstream file must be declared in the integration manifest and contain only the minimum adapter code.
