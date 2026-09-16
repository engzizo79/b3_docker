# B3 Hive — Design Language Refinement Report (Independent Review)

*Source: researcher-profile subordinate, 2026-09-16. No files written by reviewer; this is the verbatim report captured for implementation reference.*

## Priority summary (14 gaps, do these first)

| # | Gap | Impact | Section |
|---|---|---|---|
| 1 | Light-theme accent as text fails contrast (2.25:1); add `--accent-text` | A11y + first-impression | §1, §7 |
| 2 | `badge-info` specced but unimplemented; `btn-success` used but unspecced | Live bug | §2 |
| 3 | No Confirmation component; 5× `window.confirm` on irreversible actions | Security UX | §2, §7, §8 |
| 4 | Missing components: Address, AmountInput, Callout, Modal, Tabs, Dropdown, Disclosure, CodeBlock, CopyField | Blocks 115→0 | §2 |
| 5 | 9 inline patterns with no class (truncation width, break-all, flex-1, m-0, dropdown positioning) | Blocks 115→0 | §3 |
| 6 | Breakpoints as CSS vars are unusable in `@media` | Structural | §1.4 |
| 7 | Density defined but no application mechanism; touch-target rule unenforced | Consistency | §1.3, §7 |
| 8 | `display: revert` in `simple-only` is fragile | Migration risk | §3 |
| 9 | No wallet-state/sync-state axis alongside mode axis | Orthogonal states | §6 |
| 10 | Voice rules miss irreversible-action copy, error leakage, 9dp formatting, address truncation, risk honesty | Security + clarity | §5 |
| 11 | `autostake target` in Simple is a footgun | Mode correctness | §4 |
| 12 | Two docs disagree on raw-data disclosure (Disclosure vs Advanced) | Consistency | §6 |
| 13 | Refactor step 3 is a big-bang live-UI migration; sequence per-view with screenshot baseline | Delivery risk | §8 |
| 14 | No focus-visible, ARIA, or reduced-motion codified | A11y | §7 |

## Key findings detail

### Contrast (verified, computed from hex tokens)
- Light `--accent` (#c4a030) on `--bg` (#f5f3ef) = 2.25:1 — FAIL (needs 4.5:1)
- Light `.btn-secondary` text = 2.27:1 — FAIL
- Light `--text-dim` on bg (placeholder) = 2.86:1 — FAIL
- Dark `--text-dim` placeholder = 3.13:1 — FAIL
- Fix: add `--accent-text` tuned per theme; raise `--text-dim` or stop using for placeholders

### Missing tokens
- `--radius-pill` (badges hardcode 999px)
- `--focus-ring` (no :focus-visible anywhere)
- `--duration-*` / `--ease-*` (motion hardcoded)
- `--z-*` scale (50/100/200/300)
- `--shadow-sm` / `--shadow-lg`
- `--accent-text`, `--text-2xl` (hero balance), `--leading-*` (line-height)
- `--min-target` (44px/36px enforcement)

### Component canon needs expansion (10 → ~19)
Add: Tabs (2.11), Modal (2.12), Disclosure (2.13), CodeBlock (2.14), CopyField (2.15), Address (2.16), AmountInput (2.17), Callout (2.18), Confirmation (2.19), Dropdown (2.20)

### Missing utility classes (9 patterns)
`.break-all`, `.clickable`, `.nowrap`, `.flex-1`, `.m-0`, `.w-truncate` (120px), `.min-w-form` (180px), `.min-h-screen`, `.dropdown` positioning

### Mode matrix fixes
- Move `autostake target + reserve` to Advanced (silently locks funds)
- Raw data: use per-card Disclosure (not Advanced mode) — resolves UX_DESIGN vs DESIGN_LANGUAGE contradiction
- Default mode: Simple on first run; persist per-node
- Map personas: Nina/Ella→Simple, Sam→Simple+staking, Omar→Advanced
- Replace per-section "Switch to Advanced" prompts with single header pill (avoid 7 prompts cluttering Simple)

### Voice rules to add
1. Irreversible-action confirm restates amount+address+address+"cannot be undone"
2. Errors: plain language, no RPC method names/stack traces/leaked detail
3. Amounts: Decimal, 9dp, thousands separators, " B3" suffix
4. Addresses truncate by character (first 8…last 6), not CSS width
5. Raw RPC JSON never primary content — behind Disclosure
6. "Staking" as nav noun, "Earn rewards" as action verb
7. Risk trade-offs stated where the toggle is, not buried in docs

### A11y baseline (new section)
- `:focus-visible` ring on all interactive components
- ARIA roles: Tabs (tablist/aria-selected), Modal (role=dialog/aria-modal/focus-trap/ESC), Dropdown (aria-expanded/aria-haspopup), Toast (role=status/aria-live)
- `prefers-reduced-motion` fallback for all animations
- Touch targets: min-height var(--min-target) on .btn, .nav-tab, .theme-toggle
- Status never color-only (pair with icon/word for color-blind)

### Security UX
- BAN `window.confirm` for wallet-affecting actions — use Confirmation component (modal)
- Confirmation restates amount + destination + irreversibility + requires CSRF/confirm-token
- Idle-session re-auth for wallet-affecting actions
- Backend returns user-safe error codes; frontend maps to copy (no RPC leakage)
- Address validation feedback before preview (P2PKH prefix/length check)

### Refactor order (revised, safer)
0. Baseline: land D1-D6 fixes; screenshot every view at 375px+1920px both themes; add data-wallet/data-sync alongside data-mode
1. Per-view interleaved: Login→Dashboard→Wallet→Send→Staking→Batch→Settings→Wizard — (a) add tokens/classes/components that view needs (b) migrate inline styles (c) screenshot-diff (d) commit
2. Mode-visibility CSS (fix display:revert→display:block) + density-from-attribute
3. Tag surfaces per view (interleaved)
4. Replace confirm() during Send/Batch/Settings passes; lint-ban window.confirm
5. A11y pass per view
6. Verification gate per view (run checklist before next)
7. Final: 5 personas × Simple/Advanced × wallet-states × {375,1920} × {dark,light}
