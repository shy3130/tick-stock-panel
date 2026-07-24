# Stock Analysis Report Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a clearly labeled, mobile-friendly “复制报告” action to completed and historical stock-analysis reports, with clipboard fallback and visible success/failure feedback.

**Architecture:** A focused `copyText` utility owns modern Clipboard API and legacy textarea fallback behavior. `StockAnalysisDialog` owns only visibility and transient UI status. A component behavior test covers completed, streaming, success, fallback, and failure states.

**Tech Stack:** React 18, TypeScript, Vitest, Testing Library, Tailwind CSS, Vite.

## Global Constraints

- Copy the original Markdown report text; do not copy rendered HTML.
- Show the action only when content exists and generation is no longer running.
- Prefer `navigator.clipboard.writeText`; fall back to a temporary read-only textarea and `document.execCommand('copy')`.
- Show “已复制” on success and “复制失败” on failure, then restore “复制报告”.
- Do not modify report generation, persistence, history APIs, or backend code.
- The title-bar action must remain usable at mobile widths.

---

### Task 1: Register the authoritative copy requirement

**Files:**
- Create: `docs/specs/stock-analysis-report-copy.md`
- Create: `docs/acceptance/stock-analysis-report-copy.md`
- Create: `docs/reviews/stock-analysis-report-copy.md`
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-07-21-stock-analysis-report-copy-design.md`.
- Produces: `REQ-STOCK-ANALYSIS-REPORT-COPY-001` mapped to the implementation and executable test.

- [ ] **Step 1: Add the authoritative requirement**

```yaml
- id: SPEC-STOCK-ANALYSIS-REPORT-COPY-001
  path: docs/specs/stock-analysis-report-copy.md
  status: authoritative
  requirements: [REQ-STOCK-ANALYSIS-REPORT-COPY-001]
```

The requirement states that completed/current and historical reports expose a labeled title-bar copy action, copy Markdown, use fallback behavior, and report success/failure.

- [ ] **Step 2: Add traceability**

```yaml
- id: REQ-STOCK-ANALYSIS-REPORT-COPY-001
  specification: SPEC-STOCK-ANALYSIS-REPORT-COPY-001
  implementation:
    - frontend/src/lib/copyText.ts
    - frontend/src/components/stock-analysis/StockAnalysisDialog.tsx
  tests: {path: frontend/src/components/stock-analysis/StockAnalysisDialog.test.tsx, type: executable-test}
  acceptance:
    - {path: docs/acceptance/stock-analysis-report-copy.md, type: semantic-acceptance}
    - {path: docs/reviews/stock-analysis-report-copy.md, type: independent-review}
```

- [ ] **Step 3: Run the compliance checker**

Run: `python scripts/check_spec_compliance.py`

Expected: `Specification compliance passed.`

### Task 2: Implement clipboard compatibility with TDD

**Files:**
- Create: `frontend/src/lib/copyText.ts`
- Create: `frontend/src/lib/copyText.test.ts`

**Interfaces:**
- Produces: `copyText(text: string): Promise<boolean>`; resolves `true` when modern or fallback copying succeeds, otherwise `false`.

- [ ] **Step 1: Write failing utility tests**

```ts
it('uses navigator clipboard when available', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  expect(await copyText('# report')).toBe(true)
  expect(writeText).toHaveBeenCalledWith('# report')
})

it('falls back to a temporary textarea when clipboard fails', async () => {
  const execCommand = vi.fn().mockReturnValue(true)
  Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
  vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
  expect(await copyText('# report')).toBe(true)
  expect(execCommand).toHaveBeenCalledWith('copy')
  expect(document.querySelector('textarea')).toBeNull()
})
```

- [ ] **Step 2: Run tests and verify RED**

Run: `pnpm test --run src/lib/copyText.test.ts`

Expected: FAIL because `copyText.ts` does not exist.

- [ ] **Step 3: Implement the minimal utility**

```ts
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* use fallback */ }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.readOnly = true
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    return document.execCommand?.('copy') === true
  } catch {
    return false
  } finally {
    textarea.remove()
  }
}
```

- [ ] **Step 4: Run utility tests and verify GREEN**

Run: `pnpm test --run src/lib/copyText.test.ts`

Expected: all utility tests pass.

### Task 3: Add the labeled report action with TDD

**Files:**
- Create: `frontend/src/components/stock-analysis/StockAnalysisDialog.test.tsx`
- Modify: `frontend/src/components/stock-analysis/StockAnalysisDialog.tsx`

**Interfaces:**
- Consumes: `copyText(text: string): Promise<boolean>`.
- Produces: title-bar button with accessible names `复制报告`, `已复制`, or `复制失败`.

- [ ] **Step 1: Write failing component tests**

```tsx
it('copies the full completed Markdown and shows success feedback', async () => {
  copyTextMock.mockResolvedValue(true)
  render(<StockAnalysisDialog task={completedTask} mode="active" minimized={false} />)
  await userEvent.click(screen.getByRole('button', { name: '复制报告' }))
  expect(copyTextMock).toHaveBeenCalledWith('# 完整报告')
  expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument()
})

it('hides copy while streaming and reports failure', async () => {
  const { rerender } = render(<StockAnalysisDialog task={streamingTask} mode="active" minimized={false} />)
  expect(screen.queryByRole('button', { name: '复制报告' })).not.toBeInTheDocument()
  copyTextMock.mockResolvedValue(false)
  rerender(<StockAnalysisDialog task={completedTask} mode="active" minimized={false} />)
  await userEvent.click(screen.getByRole('button', { name: '复制报告' }))
  expect(screen.getByRole('button', { name: '复制失败' })).toBeInTheDocument()
})
```

- [ ] **Step 2: Run component tests and verify RED**

Run: `pnpm test --run src/components/stock-analysis/StockAnalysisDialog.test.tsx`

Expected: FAIL because the existing action is icon-only and ignores failure.

- [ ] **Step 3: Implement the labeled button and status**

Replace the local clipboard call with `copyText(content)`. Store status as `'idle' | 'success' | 'error'`, render compact text beside the icon, add `aria-label`, and reset status after 2 seconds. Keep the existing `content && !isWorking` visibility condition.

- [ ] **Step 4: Run component tests and verify GREEN**

Run: `pnpm test --run src/components/stock-analysis/StockAnalysisDialog.test.tsx`

Expected: all component tests pass.

### Task 4: Verify, review, and deploy

**Files:**
- Modify: `docs/acceptance/stock-analysis-report-copy.md`
- Modify: `docs/reviews/stock-analysis-report-copy.md`

**Interfaces:**
- Consumes: completed utility and component behavior.
- Produces: test/build/spec evidence and deployed production artifact.

- [ ] **Step 1: Run focused and existing stock-analysis tests**

Run: `pnpm test --run src/lib/copyText.test.ts src/components/stock-analysis/StockAnalysisDialog.test.tsx src/pages/stock-analysis-five-dimension.test.tsx`

Expected: all tests pass.

- [ ] **Step 2: Run production build**

Run: `pnpm build`

Expected: TypeScript and Vite complete with exit code 0.

- [ ] **Step 3: Run specification and diff checks**

Run: `python scripts/check_spec_compliance.py` and `git diff --check`.

Expected: compliance passes and diff check emits no errors.

- [ ] **Step 4: Complete independent review**

Record requirement-to-code, test, mobile visibility, fallback, failure feedback, and production evidence in the acceptance/review documents.

- [ ] **Step 5: Commit and deploy**

```bash
git add frontend/src/lib/copyText.ts frontend/src/lib/copyText.test.ts \
  frontend/src/components/stock-analysis/StockAnalysisDialog.tsx \
  frontend/src/components/stock-analysis/StockAnalysisDialog.test.tsx docs
git commit -m "feat: add visible report copy action"
```

Deploy a versioned TickFlow image to `192.168.10.28:3018`, then verify the container image, page health, and button behavior at mobile width.
