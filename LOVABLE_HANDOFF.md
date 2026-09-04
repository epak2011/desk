# Trading Desk → Lovable frontend handoff

This is the copy-paste package for building the next Trading Desk frontend in
Lovable. It intentionally contains no credentials, private portfolio data, or
production database connection strings.

## What remains authoritative

- The Python rules engine remains the only source of investment decisions.
- Supabase Auth remains the identity provider.
- Supabase/Postgres remains the durable store.
- The worker remains responsible for market refreshes, rule execution, outcome
  scoring, and notifications.
- The frontend renders API responses. It must not calculate, reinterpret, or
  silently override actions, triggers, invalidations, confidence, position size,
  data trust, or market regime.
- The existing Streamlit app remains the internal calibration and fallback tool
  while the new client is built.

## Files to give Lovable

1. `LOVABLE_HANDOFF.md` — product and build prompt.
2. `contracts/openapi.yaml` — exact frontend/API boundary.
3. `contracts/examples/decision.json` — fictional decision response.
4. `contracts/examples/regime.json` — fictional market-regime response.
5. `contracts/examples/workspace.json` — fictional private-workspace response.

Do not upload `.streamlit/secrets.toml`, `.env`, database URLs, service-role
keys, email provider keys, private user state, logs, or production exports.

## Implementation sequence

1. Generate the responsive shell, landing page, authentication screens, and
   mocked Analyze workflow from the supplied fixtures.
2. Connect Supabase Auth using Lovable's secret/environment-variable system.
3. Connect read-only `/v1/regime` and `/v1/decisions/{ticker}` endpoints.
4. Connect authenticated `/v1/workspace`, `/v1/watchlist`, `/v1/attention`, and
   `/v1/portfolio` endpoints.
5. Add mutations only after row-level security and two-user isolation tests pass.
6. Keep Calibration and System Health owner/operator-only.

## Required environment variables

Use placeholders during generation. Never hard-code values.

```text
VITE_SUPABASE_URL
VITE_SUPABASE_ANON_KEY
VITE_TRADING_DESK_API_URL
```

The Supabase anon key is intended for browser clients. The Supabase service-role
key and `DATABASE_URL` are server-only and must never be exposed to Lovable's
browser bundle.

## Authentication and ownership rules

- Use the official Supabase client and persist/refresh sessions normally.
- Send the current Supabase access token as `Authorization: Bearer <JWT>` to
  authenticated API routes.
- The API must derive `user_id` from the verified token. Never accept a user ID,
  email address, or owner flag supplied by the browser.
- Private state is keyed by the authenticated Supabase UUID and protected with
  row-level security.
- Shared data: market snapshots, rule outputs, regime, non-personal research.
- Private data: watchlist, holdings, notes, sizing settings, notification
  preferences, and user decision history.
- The public demo must use fictional/sample state, must not write, and must not
  expose Holdings, System Health, private notes, or private history.
- Do not place tokens in query parameters or logs.
- On `401`, clear stale local auth state and return to sign-in without losing the
  requested destination.

## Product behavior

Every stock page should answer these questions in order:

1. What should I do?
2. How much should I own or add now?
3. What exact price or event makes the decision actionable?
4. What invalidates it?
5. Why is this the current view?
6. What changed since the prior decision?
7. Can I trust the data?

Action semantics:

- `enter_now`: enough evidence and upside to justify initiating exposure.
- `accumulate`: add gradually; conditions are constructive but staging matters.
- `watch`: no directional verdict yet; wait for the named trigger.
- `hold_off`: evidence or timing is not ready; do not initiate/add yet.
- `avoid`: current risk/reward does not justify exposure.

Never score or describe Watch/Hold Off as failed directional predictions. They
are patience/trigger states.

## Required screens

### Public landing and sign-in

- Headline: “Meet your investment assistant.”
- Subhead: “Trading Desk helps you make sense of any stock—from the big picture
  to what matters next.”
- The product preview is the visual priority; sign-in is secondary.
- Primary public CTA: “Try it with a stock →” with “No account needed.”
- Include a realistic fictional decision preview showing:
  - Thinking of buying? Wait
  - Already own it? Hold
  - Business: Strong
  - Trend: Holding
  - Entry: Too extended
  - An exact fictional trigger and invalidation level
- Sign-in card copy: “Welcome back” and “Sign in to pick up where you left off.”
- Create-account flow requires an unchecked consent checkbox:
  “I understand that Trading Desk does not provide investment advice and is for
  educational and informational purposes only. I agree to the Terms and
  Conditions and Privacy Policy.”
- Do not enable account creation until the checkbox is checked.

### Application shell

- Desktop: compact left navigation; mobile: conventional hamburger drawer.
- Primary destinations: Market, Analyze, Watchlist, Alerts, Portfolio, Ideas.
- Secondary/operator destinations: Calibration, System Health, Methodology.
- Signed-in profile is compact and placed at the bottom of navigation.
- Active page uses a soft blue background and clear icon.
- No beige. Use cool white, blue-gray, blue, and restrained green/red status
  accents.
- Mobile users must always have a visible way to reopen navigation.

### Analyze

- Put the action first and make it unmistakable.
- Separate “Thinking of buying?” from “Already own it?” when they differ.
- Always display the exact trigger price/event and invalidation price/event.
- Display `executable: false` as a blocking data-quality state, not a subtle note.
- Show products, customers, business model, and strategic position in a concise,
  company-specific overview without duplicating ticker metadata.
- Never truncate the overview with an ellipsis.
- Put detailed freshness and worker information behind a small Data Quality
  control in the upper-right.
- Never invent missing values. Use a plain unavailable state.

### Market

- Lead with current opportunity action, entry timing, what changed, and a daily
  context-specific “Why today.”
- Show drivers, risks, and exact watch triggers.
- Make timeframe distinctions explicit.
- Crypto cycle, medium-term trend, and tactical entry timing are separate fields;
  never collapse them into one “phase.”

### Watchlist and Alerts

- Watchlist rows show price, daily change, action, confidence, trigger,
  invalidation, attention state, and freshness.
- Alerts show only current material events. Expired, superseded, duplicate, or
  no-longer-actionable events must not remain in the primary inbox.
- Clicking an item opens the corresponding Analyze page without losing auth.

### Portfolio and Ideas

- Portfolio is authenticated and user-scoped.
- Position-aware advice is decision support, never personalized investment advice.
- Notes and ideas are private by default.
- Use optimistic updates only when rollback/error states are implemented.

### Calibration and System Health

- Treat these as operator/debugging surfaces, not public marketing pages.
- Calibration answers which rule families add or destroy edge and under what
  conditions.
- Enter/Accumulate and Avoid are directional families. Watch/Hold Off are
  evaluated as trigger/patience systems.
- Show sample counts with every percentage and label immature results clearly.
- Do not automatically change trading rules from frontend observations.

## Response and failure rules

- Render `meta.data_as_of`, `meta.freshness`, `meta.engine_version`, and
  `meta.contract_version` consistently.
- `freshness: stale`: keep historical content visible but show a prominent stale
  warning and disable actions requiring current data.
- `executable: false`: display the decision as non-actionable and explain the
  data-trust issue.
- `404`: offer another ticker; do not show a generic crash page.
- `422`: explain the ticker is invalid or unsupported.
- `409`: refetch the workspace, reconcile, and preserve the user's unsaved input.
- `503` or `retryable: true`: preserve the page, show a retry control, and never
  replace the canonical action with a client guess.
- Every async surface needs loading, empty, success, partial-data, stale-data,
  unauthorized, and retryable-error states.

## Visual direction

- Serious personal decision workstation, not a consumer-fintech toy, debug
  dashboard, or documentation site.
- Confident simplicity, dense enough for an investor but understandable to a
  retail user.
- Clear hierarchy, restrained shadows, slightly rounded components, and a
  consistent 8px spacing system.
- Avoid unexplained charts, decorative financial imagery, gradients everywhere,
  emoji-heavy navigation, and jargon without definitions.
- Meet WCAG AA contrast, keyboard navigation, visible focus, semantic headings,
  reduced-motion preferences, and 44px mobile touch targets.

## Acceptance tests before connecting production data

- Desktop, half-screen, tablet, and mobile layouts have no horizontal overflow.
- Hamburger opens, closes, traps focus appropriately, and can always be reopened.
- Refresh and page changes do not sign the user out.
- Two test users cannot read or mutate each other's private state.
- Public demo cannot write private state.
- Analyze renders the same canonical receipt/action as the Streamlit fallback.
- Trigger and invalidation prices match the receipt exactly.
- Stale or blocked data cannot look executable.
- Empty watchlist, missing research, unsupported ticker, expired session, API
  outage, and workspace conflict states are usable.
- No secrets or private data appear in the browser bundle, URLs, analytics, or
  console logs.

## Copy-paste prompt for Lovable

```text
Build a production-quality responsive web frontend for Trading Desk, a serious
investment decision-support application for retail investors. Use React,
TypeScript, Tailwind, shadcn/ui, React Query, React Router, and the official
Supabase JavaScript client.

I am supplying an OpenAPI 3.1 contract and three fictional JSON fixtures. Treat
contracts/openapi.yaml as authoritative. Generate a typed API client from it or
create equivalent strongly typed interfaces. Build against the fixtures first,
with a clean adapter that switches to VITE_TRADING_DESK_API_URL later.

Critical architecture rule: the frontend must never calculate or reinterpret a
Trading Desk action, market regime, trigger, invalidation, confidence, position
size, or data-trust result. The Python backend is the only decision engine. The
frontend renders canonical API responses and their timestamps/version metadata.

Use Supabase Auth. Send the access token as a Bearer token to authenticated API
routes. Never send a browser-supplied user_id or email to select private data.
Never expose a service-role key or database URL. Public demo mode is read-only
and uses only supplied fictional fixtures.

Create these routes:
/                  public landing/sign-in
/market            market regime
/analyze/:ticker   stock decision memo
/watchlist         saved symbols
/alerts            current attention inbox
/portfolio         holdings and position-aware context
/ideas             private notes/ideas
/calibration       operator calibration
/health            operator system health
/methodology       methodology and educational disclosures

Landing page:
- Headline: “Meet your investment assistant.”
- Subhead: “Trading Desk helps you make sense of any stock—from the big picture
  to what matters next.”
- Make a realistic fictional product preview the dominant element.
- Primary CTA: “Try it with a stock →”; subtext: “No account needed.”
- Put a smaller “Welcome back” sign-in card beside it.
- The create-account form must require an unchecked educational-use/Terms/Privacy
  consent checkbox before submission.
- State clearly that Trading Desk does not provide investment advice and all
  information is for educational and informational purposes only.

App shell:
- Desktop compact left navigation; mobile conventional three-line hamburger
  drawer that can always be reopened.
- Pages: Market, Analyze, Watchlist, Alerts, Portfolio, Ideas; secondary:
  Calibration, System Health, Methodology.
- Compact profile control at the bottom.
- Soft-blue active state. No beige anywhere.

Analyze hierarchy:
1. What should I do?
2. How much now?
3. Exact trigger price/event.
4. Exact invalidation price/event.
5. Why this view?
6. What changed?
7. Can I trust the data?

Map action values to user-facing labels without changing semantics:
enter_now → Enter; accumulate → Accumulate; watch → Watch; hold_off → Hold Off;
avoid → Avoid. Separate “Thinking of buying?” and “Already own it?” when useful.
Treat Watch/Hold Off as patience/trigger states, not directional predictions.

Implement loading, empty, partial, stale, blocked, unauthorized, conflict, and
retryable-error states. If executable is false, clearly block actionability. Show
data freshness and engine version without turning the product into a debug
dashboard. Never invent missing financial values.

Use a cool white/blue-gray canvas, bright but restrained blue and green accents,
an 8px spacing system, accessible contrast, visible focus states, semantic HTML,
keyboard support, reduced motion, and 44px mobile targets. The result should feel
like a polished, credible personal investment workstation—not a Streamlit app,
consumer trading toy, or documentation page.

First deliver the complete fixture-backed experience and a concise integration
checklist. Do not connect production mutations until two-user Supabase row-level
security isolation has been tested.
```

## Definition of done for the frontend branch

The first frontend milestone is complete when a user can sign in, analyze a
ticker, see an exact trigger and invalidation, add/remove it from their private
watchlist, refresh without losing the session, and use the same workflow on
desktop and mobile—with canonical results matching the existing engine.
