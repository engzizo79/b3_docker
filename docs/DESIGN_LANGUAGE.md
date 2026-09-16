# B3 Hive Design Language

> The grammar the whole UI obeys. This version incorporates the independent design review (`DESIGN_REVIEW.md`, 14 gaps): a complete token layer, a 19-component canon, an accessibility baseline, security-UX rules, and a **single global Simple/Advanced mode toggle**.

## 0. Core Principles

1. **One global mode toggle** — Simple or Advanced for the entire wallet UI. Set once in Settings or via the header pill. **No per-page mode controls, no per-section "Switch to Advanced" prompts.** One toggle, one place.
2. **Simple never hides money — only jargon and footguns.** A noob sees balances, send, receive, stake on/off, unstake (guided), password. Everything that can lose coins if misconfigured (coin control, custom fees, batch, validator ops, autostake target, raw RPC) lives in Advanced.
3. **Raw data behind per-card Disclosure**, never primary. Reachable in both modes — operators in Simple can still reach raw details via the caret, no mode switch needed.
4. **Every irreversible action goes through a Confirmation dialog** that restates amount + destination + irreversibility. `window.confirm()` is banned for wallet-affecting actions.
5. **No RPC leakage to the browser.** Errors are plain-language + actionable; never surface RPC method names, stack traces, or raw `detail` strings.
6. **Status is never color-only.** Pair every color signal with an icon or word (color-blind safety).

## 1. Token Layer

All tokens live in `theme.css`. No hardcoded values outside that file.

### 1.1 Spacing Scale

| Token | Value | Legacy mapping |
|---|---|---|
| `--space-1` | 0.25rem (4px) | |
| `--space-1-5` | 0.375rem (6px) | 0.3rem → `--space-1` |
| `--space-2` | 0.5rem (8px) | 0.6rem → `--space-2` |
| `--space-3` | 0.75rem (12px) | |
| `--space-4` | 1rem (16px) | |
| `--space-5` | 1.5rem (24px) | |
| `--space-6` | 2rem (32px) | |

**Rule:** No `margin`/`padding` literals outside `theme.css`. Legacy values map deterministically per the right column.

### 1.2 Type Scale

These tokens are **introduced in refactor step 1**; `theme.css` currently hardcodes the same values on `h1`/`h2`/`h3` and `.text-sm`/`.text-xs`.

| Token | Value | Usage |
|---|---|---|
| `--text-xs` | 0.75rem (12px) | labels, badges, hints |
| `--text-sm` | 0.875rem (14px) | body secondary, inputs |
| `--text-base` | 1rem (16px) | body primary |
| `--text-md` | 1.1rem (17.6px) | card titles (h3) — non-modular, matches existing h3; do not "fix" |
| `--text-lg` | 1.25rem (20px) | section titles (h2), stat values |
| `--text-xl` | 1.5rem (24px) | page titles (h1) |
| `--text-2xl` | 2rem (32px) | hero balance (dashboard) |

Line-height tokens: `--leading-tight: 1.2`, `--leading-normal: 1.5`, `--leading-relaxed: 1.6`.

### 1.3 Density — driven by attribute, not modifier class

Density is applied via `[data-mode="advanced"]` selectors, **not** a per-card `--dense` modifier (which would require per-element JS toggling).

| Mode | Card padding | Input padding | Stat gap | Touch target |
|---|---|---|---|---|
| Simple (default) | `--space-4` | `--space-2 --space-3` | `--space-3` | `--min-target: 44px` |
| Advanced | `--space-3` | `--space-2` | `--space-2` | `--min-target-sm: 36px` |

```css
[data-mode="advanced"] .card { padding: var(--space-3); }
[data-mode="advanced"] .input { padding: var(--space-2); }
[data-mode="advanced"] .stat-grid { gap: var(--space-2); }
```

`--min-target` and `--min-target-sm` are enforced as `min-height` on `.btn`, `.nav-tab`, `.theme-toggle`, `.btn-sm` (sm uses `--min-target-sm`). The rule is no longer aspirational.

### 1.4 Breakpoints — documented constants, NOT CSS custom properties

Media queries cannot consume `var()` — `@media (min-width: var(--bp-sm))` is invalid CSS. Breakpoints are **documented constants** inlined in `@media` rules:

| Constant | Value |
|---|---|
| `BP-SM` | 640px (grid collapse to 1 col) |
| `BP-MD` | 768px (tablet) |
| `BP-LG` | 1024px (desktop) |

`theme.css` must implement all three (currently only 640px exists). Use the literal in `@media (max-width: 640px)` etc.

### 1.5 Tokens added by the review

| Token | Purpose |
|---|---|
| `--accent-text` | Legible-on-background accent (light: darken to ~#8a6a00; dark: keep #E8C05A) — fixes 2.25:1 contrast |
| `--text-2xl` | Hero balance |
| `--leading-*` | Line-height |
| `--radius-pill` | Badges (replaces hardcoded 999px) |
| `--focus-ring` | `0 0 0 2px var(--bg), 0 0 0 4px var(--accent)` — a11y focus |
| `--duration-fast: 0.15s` / `--duration-med: 0.2s` / `--duration-slow: 0.3s` | Motion |
| `--ease-out: cubic-bezier(0.16,1,0.3,1)` | Motion |
| `--z-50` / `--z-100` / `--z-200` / `--z-300` | Dropdowns, modals, toasts, header |
| `--shadow-sm` / `--shadow-lg` | Replaces single `--shadow` where modal/alert need more |
| `--min-target` / `--min-target-sm` | Touch target enforcement |

### 1.6 Contrast contract

All text tokens must hit **≥4.5:1** on their intended background (WCAG AA), **≥3:1** for large text (≥18.66px bold or ≥24px). Verified failures to fix: light `--accent` as text (2.25:1), light `.btn-secondary` text (2.27:1), `--text-dim` on bg (2.86:1 light / 3.13:1 dark). `--text-dim` is no longer used for placeholder text — use `--text-muted` + opacity.

## 2. Component Patterns (19)

One canonical implementation per component in `theme.css`. No inline styles. Variants via modifier classes. Each interactive component carries an ARIA contract (Section 8).

### 2.1 Card
`.card` (base) · `.card--flush` (padding 0, inner wrapping). Density via `[data-mode]`, not a modifier.

### 2.2 Button
`.btn` · `.btn-primary` · `.btn-secondary` · `.btn-danger` · `.btn-success` (**added** — was used but unspecced) · `.btn-ghost` · `.btn-sm` · `.btn-block`. Light `.btn-secondary` must use `--accent-text`, not raw `--accent`.

### 2.3 Input
`.input` (mono font, focus accent) · `.input-group` (label + hint + error wrapper). `inputmode="decimal"` on amount fields.

### 2.4 Badge
`.badge` (pill, `--radius-pill`, uppercase, 0.7rem) · `.badge-success` · `.badge-warning` · `.badge-danger` · `.badge-info` (**must be implemented** — currently specced but absent; info alerts render unstyled).

### 2.5 Stat
`.stat` (label uppercase muted + value mono bold) · `.stat-grid` (`repeat(auto-fit, minmax(120px,1fr))`) · `.stat-value-lg` for hero balance (`--text-2xl`).

### 2.6 Empty State
```html
<div class="empty-state">
  <h3>No wallet loaded</h3>
  <p>Create or load a wallet to see balances, send, and stake.</p>
  <button class="btn btn-primary">Open Setup Wizard</button>
</div>
```
Structure: **title — one sentence — one action button.** Centered, `mw-narrow` (360px). The canonical pattern for D5.

### 2.7 Banner / Syncing Strip
`.banner` (full-width, `--space-2 --space-4`, centered) · `.banner--info` / `.banner--warning` / `.banner--danger`. Shown atop every view while syncing, staking, or alert-active. Icon + color, not color-only.

### 2.8 Toast
`.toast` (`position: fixed; bottom/right; --z-300`) · `.toast-success` / `.toast-danger` / `.toast-info`. `role="status"`, `aria-live="polite"`. The inline `position:fixed` on `index.html:837` is redundant — delete it.

### 2.9 Toggle
`.toggle` (pill track + knob, accent when on) · `.toggle--sm` (inline). `role="switch"`, `aria-checked`.

### 2.10 Progress
`.progress` (track) · `.progress-bar` (fill, `width: var(--pct, 0%)`). Dynamic width via CSS var on `style="--pct:..."` — the one legitimate dynamic inline style.

### 2.11 Tabs — **added**
`.tabs` (tablist) · `.tab` (tab, `role="tab"`, `aria-selected`). Existing `.nav-tab` gets the ARIA contract it currently lacks.

### 2.12 Modal — **added**
`.modal` (overlay, `role="dialog"`, `aria-modal="true"`) · `.modal--sm` (360px) / `.modal--md` (440px) / `.modal--lg` (600px). Focus-trap, ESC-to-close, backdrop-click-to-close, focus restoration. Replaces `window.confirm()`.

### 2.13 Disclosure — **added**
`.disclosure` (styled `<details>`) · `.disclosure__summary` (cursor pointer, rotation chevron). Used for raw JSON / "Raw details" everywhere (resolves D4 and the Disclosure-vs-Advanced contradiction).

### 2.14 CodeBlock — **added**
`.codeblock` (mono, `--text-xs`, `overflow-x: auto`, `--space-3` padding, border) + optional copy button via CopyField. Replaces bare `<pre class="mono text-xs">` at `index.html:254,385,159`.

### 2.15 CopyField — **added**
`.copyfield` (value + button + copied-state toast). Used for addresses, signatures, recovery codes, txids at `index.html:277,293,583`.

### 2.16 Address — **added**
`.address` (mono, `--text-xs`, truncate by **character**: first 8 … last 6, full value in `title`, click-to-copy). Never truncate the version byte / `S` prefix. Replaces the most-repeated inline pattern (`max-width:120px` + ellipsis) at `index.html:218,237,238,291`.

### 2.17 AmountInput — **added**
`.amount-input` (input with fixed `B3` suffix, `inputmode="decimal"`, max-9-decimal validation, optional "max available" helper). The highest-value missing component for a 9-decimal chain — wrong decimal place = wrong amount. Advanced can show a base-unit toggle.

### 2.18 Callout — **added**
`.callout` (review/result panel: `--accent-bg`, `border-focus`, `--radius-md`) · `.callout--info` / `.callout--success` / `.callout--danger`. Absorbs ~12 inline styles at `index.html:331,353,434,466,712`. Distinct from `.banner` (full-width strip).

### 2.19 Confirmation — **added**
`.confirm` (modal-based) restating **amount + destination + `— this cannot be undone`**. Requires CSRF + confirm-token. Replaces all 5 `window.confirm()` calls (`app.js:336,364,466,541,577`). The single most important security-UX addition.

### 2.20 Dropdown — **added**
`.dropdown` (anchored, `--z-200`, `aria-expanded`, `aria-haspopup`, click-away, focus management). Replaces the hand-rolled alerts popover at `index.html:84-106`.

## 3. Utility Classes

### 3.1 Flexbox
`.flex` · `.flex-col` · `.flex-between` · `.flex-center` · `.flex-start` (align + gap-2) · `.flex-wrap` · `.flex-1` (**added**).

### 3.2 Gap
`.gap-1` … `.gap-4` (using `--space-*`).

### 3.3 Margin / Padding
`.mt-1`…`.mt-6` · `.mb-1`…`.mb-6` · `.m-0` (**added**) · `.mx-auto` · `.p-3` / `.p-4` (**added** — padding was missing).

### 3.4 Text
`.text-center` · `.text-right` · `.text-mono` · `.text-xs` · `.text-sm` · `.break-all` (**added**) · `.nowrap` (**added**) · `.clickable` (**added**, cursor pointer for interactive non-button elements).

### 3.5 Width / Height
`.mw-narrow` (360px) · `.mw-modal` (440px) · `.mw-page` (960px) · `.w-truncate` (**added**, 120px — replaced by `.address` component where possible) · `.min-w-form` (**added**, 180px) · `.min-h-screen` (**added**).

### 3.6 Mode Visibility
```css
.advanced-only { display: none; }
[data-mode="advanced"] .advanced-only { display: block; }
.simple-only { display: block; }
[data-mode="advanced"] .simple-only { display: none; }
```
**Fixed:** `display: revert` → `display: block` (revert rolls back to UA stylesheet, breaking flex/span layouts). Use `--simple-flex` variant where a flex container must stay flex.

## 4. Motion + States

| State | Transition | Duration token |
|---|---|---|
| hover (buttons, nav) | background/color | `--duration-fast` |
| focus (inputs) | border-color to accent | `--duration-fast` |
| progress bar | width | `--duration-slow` |
| toast | slide-up + fade | `--duration-med` |
| modal | fade + scale | `--duration-fast` |
| mode toggle | content swap (CSS only) | `--duration-med` |

**Loading:** spinner + disabled state on buttons. **Disabled:** opacity 0.5, `cursor: not-allowed`. **Reduced motion:** `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; } }`.

## 5. Voice

| # | Rule | Bad | Good |
|---|---|---|---|
| 1 | Plain language | "Create transaction" | "Send B3" |
| 2 | Action-oriented buttons | "Submit" | "Send" |
| 3 | Honest empty states | blank table | "No transactions yet — receive B3 to get started" |
| 4 | Amounts with units, 9dp, thousands | "1.5" | "1.500000000 B3" (backend Decimal, never float) |
| 5 | No RPC names in copy | "getstakinginfo" | "Staking details" |
| 6 | Staking: nav noun, action verb | (nav) "Earn rewards" / (button) "Staking" | nav: "Staking" · action: "Earn rewards" |
| 7 | **Irreversible confirm restates cost** | "Confirm" | "Send 1.5 B3 to S…f7Q3 — this cannot be undone" |
| 8 | **Errors: plain + actionable, no leakage** | "RpcError: -32601 wallet not loaded" | "Wallet not loaded — open the Wallet view to load it" |
| 9 | **Address truncation by character** | CSS `max-width` ellipsis | `S` + first 8 … last 6, full in `title` |
| 10 | **Raw JSON never primary** | dumped `<pre>` | human cards + Disclosure labeled "Raw details" |
| 11 | **Risk trade-offs stated at the toggle** | buried in docs | inline caveat next to the auto-unlock enable |

## 6. Simple / Advanced Mode Matrix

**Single global toggle.** One switch in Settings, one pill in the header. No per-section prompts. Persisted per-node in SQLite. Default on first run: **Simple**.

| Area | Simple mode | Advanced mode |
|---|---|---|
| Dashboard | Balance, sync, finality cards, staking rewards | (nothing hidden — raw via per-card Disclosure in both) |
| Send | Address + amount + fee (auto) | + coin control, custom fee, raw hex |
| Receive | QR + copy | + labels, derivation paths |
| Staking | Enable/disable, rewards, one-click **guided** unstake | + autostake target/reserve, weight, activation depth, consolidation tuning, auto-unlock vault, raw `getstakinginfo` |
| Wallet | Create / load | + dump, import, sign/verify |
| Batch | **hidden** | Full recipe engine |
| FN / Assets | Balances + simple market view | + validator start/stop, raw asset state |
| Settings | Password, 2FA, theme, **mode toggle**, autostake on/off | + autostake target/reserve, node config, RPC console, logs, advanced security |

**Autostake target/reserve is Advanced** (moves liquid B3 into locked STAKE outputs with activation-depth lockup — a footgun, not money visibility). Simple gets autostake **on/off** only. This honors "staking is basic" without handing newcomers a lockup trap.

**Raw data** lives behind per-card **Disclosure in both modes** — not behind Advanced. Operators in Simple can reach it without switching.

### Persona mapping
- **Nina** (newcomer) → Simple (default)
- **Ella** (everyday) → Simple
- **Sam** (staker) → Simple + staking (autostake on/off); Advanced for target tuning
- **Max** (migrator) → Simple for migration, may switch
- **Omar** (operator) → Advanced (persisted once)

### Toggle placement
- **Header:** small "Simple" / "Advanced" pill — tap to switch globally, no menu dive. The only persistent affordance.
- **Settings:** primary toggle location (persistent).
- **No per-section prompts.**

### Implementation
- `data-mode` attribute on `<html>` (like `data-theme`).
- Alpine reads `settings.advanced_mode` from backend, sets `document.documentElement.dataset.mode`.
- CSS `[data-mode="advanced"] .advanced-only { display: block }` controls visibility — no JS per-element toggling.
- Backend persists the preference in the settings table.

## 7. State Axes (orthogonal to mode)

Mode (Simple/Advanced) is one axis. Wallet state and sync state are two more, handled by Alpine `x-if`/`x-show` today — to be tokenized as attributes so CSS can drive visibility without scattered conditionals.

| Attribute | Values | Example CSS |
|---|---|---|
| `data-mode` | `simple` / `advanced` | `[data-mode="advanced"] .advanced-only` |
| `data-wallet` | `loaded` / `none` | `[data-wallet="none"] .wallet-only { display: none }` |
| `data-sync` | `synced` / `ibd` / `stopped` | `[data-sync="ibd"] .sync-banner { display: block }` |

Alpine sets these on `<html>` alongside `data-theme`/`data-mode`. Views declare membership via `.wallet-only` / `.no-wallet-only` / `.syncing-only` classes, mirroring `advanced-only`.

## 8. Accessibility Baseline

| Rule | Implementation |
|---|---|
| Focus-visible | Global `:focus-visible { outline: none; box-shadow: var(--focus-ring); }` on every interactive component |
| ARIA — Tabs | `role="tablist"`, `.tab role="tab" aria-selected` |
| ARIA — Modal | `role="dialog"`, `aria-modal="true"`, focus-trap, ESC, backdrop-click, focus restoration |
| ARIA — Dropdown | `aria-expanded`, `aria-haspopup="menu"`, click-away, focus management |
| ARIA — Toggle | `role="switch"`, `aria-checked` |
| ARIA — Toast | `role="status"`, `aria-live="polite"` |
| ARIA — Disclosure | native `<details>`/`<summary>` (semantic by default) |
| Reduced motion | `@media (prefers-reduced-motion: reduce)` kills animations |
| Touch targets | `min-height: var(--min-target)` on `.btn`, `.nav-tab`, `.theme-toggle`; `--min-target-sm` on `.btn-sm` |
| Color + icon | Status never color-only — pair every badge/banner with an icon or word |
| Contrast | ≥4.5:1 text, ≥3:1 large (Section 1.6) |

## 9. Security UX

| Rule | Detail |
|---|---|
| `window.confirm()` banned | All wallet-affecting / irreversible actions use the Confirmation component (2.19) |
| Confirmation restates | amount + destination + `— this cannot be undone` + CSRF/confirm-token |
| Idle re-auth | After N minutes idle, require re-auth (password or 2FA) for any wallet-affecting action; modal, not silent redirect |
| Error codes | Backend returns user-safe error codes; frontend maps to copy — no RPC method names, stack traces, or raw `detail` |
| Address validation | P2PKH prefix/length check (version byte 0x3F, `S` prefix) **before** preview, inline success/failure feedback |
| Risk honesty | Auto-unlock caveat stated at the toggle, not buried in docs |

## 10. Responsive Rules

| Width | Behavior |
|---|---|
| < 640px (mobile) | Single column, nav scrolls horizontally, stat-grid collapses, wizard steps stack |
| 640–768px | 2-column grids |
| 768–1024px | 2-column grids, comfortable |
| > 1024px (desktop) | Full stat-grid, density per mode |

Touch targets: `--min-target` (44px) in Simple, `--min-target-sm` (36px) in Advanced.

## 11. Verification Checklist

- [ ] Zero inline `style=` in `index.html` (current: 115, target: 0; exception: `--pct` on progress bars)
- [ ] Mobile 375px: no horizontal overflow, touch targets ≥ 44px (Simple)
- [ ] Desktop 1920px: no sparse stretch, density appropriate
- [ ] 5 persona journeys walkable in both Simple and Advanced
- [ ] No hex literals outside `theme.css`
| [ ] Every component matches its canonical pattern (Section 2, 19 components)
- [ ] Voice rules (Section 5, 11 rules) applied to all copy
- [ ] `window.confirm()` count = 0 for wallet-affecting actions
- [ ] Contrast ≥4.5:1 on all text pairs, both themes
- [ ] `:focus-visible` visible on all interactive components
- [ ] ARIA roles on tabs, modals, dropdowns, toasts, toggles
- [ ] Status signals are color + icon, never color-only

## 12. Refactor Order (revised, per-view)

0. **Baseline + prerequisites.** Land D1–D6 backend/flow fixes (already done for D1/D2/D3/D5). Capture screenshots at 375px + 1920px for both themes, every view, as the regression baseline. Add `data-wallet`/`data-sync` alongside `data-mode` on `<html>`.
1. **Extend `theme.css`** with all new tokens (Section 1.5) + the 19 component classes + utility classes (Section 3) + mode-visibility CSS (fixed `display:block`) + density-from-attribute + a11y baseline. Fix `badge-info`, add `btn-success`.
2. **Per-view migration** (Login → Dashboard → Wallet → Send → Staking → Batch → Settings → Wizard): for each view, (a) add the classes/components that view needs, (b) migrate its inline styles, (c) screenshot-diff at 375 + 1920 both themes, (d) commit. Shippable after every view.
3. **Confirmation component** replaces `window.confirm()` during the Send/Batch/Settings passes. Lint-ban `window.confirm`.
4. **A11y pass per view**: focus-visible, ARIA roles, touch-target min-height, contrast fixes (light `--accent-text`, `--text-dim`), color-only→icon+color.
5. **Mode toggle + state axes**: header pill + Settings toggle + `data-mode`/`data-wallet`/`data-sync` attributes + tag Advanced/wallet-only surfaces per view.
6. **Verification gate per view**: run the Section 11 checklist for that view before moving on.
7. **Final cross-view walkthrough**: 5 personas × Simple/Advanced × {no-wallet, locked, unlocked, syncing} × {375px, 1920px} × {dark, light}.
8. **Resume feature work** (staking S1–S5, FN/Assets, versions, logs) inside the established system.
