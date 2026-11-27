# web

The map-first dashboard: React 19 + TypeScript + Vite + MapLibre GL JS, hosted on
Cloudflare Pages (free). It reads everything through the API in
[`docs/contracts/openapi.yaml`](../docs/contracts/openapi.yaml). The look and behaviour
follow the 13 approved files in [`design/project/`](../design/project/).

There is no sample data in the app. Every number, name and status on screen comes from the
API; a new workspace shows the Empty screen with the real global feed signals
(`GET /v1/map/events`) and the real feed health (`GET /v1/feeds/health`). Mock data exists
only in `tests/` (MSW), and CI fails if the designs' sample names or the test fixtures
appear in the built bundle.

## Run it

```bash
cd web
npm ci
VITE_API_URL=http://localhost:8000 npm run dev      # the API from `docker compose up`
```

| Script | What it does |
|---|---|
| `npm run gen:api` | Regenerate `src/api/schema.d.ts` from the contract (`openapi-typescript`). CI fails if it is out of date. |
| `npm run typecheck` / `npm run lint` / `npm test` | `tsc`, ESLint, Vitest (+ MSW) |
| `npm run build` | Typecheck, then a production build in `dist/` |
| `npm run check:bundle` | Fails on design sample content or fixtures in `dist/`; prints every file's size |
| `npm run e2e` | Playwright screenshots of every screen (after `VITE_API_URL=http://api.test npm run build`) |
| `npm run map-data` | Rebuild `public/map/` from Natural Earth and the World Port Index (rarely needed) |

`VITE_API_URL` is the API's base URL without `/v1`, read at build time (the root
`.env.example`'s `VITE_API_BASE_URL` also works). Without it the app says so instead of
guessing.

## Deploy (Cloudflare Pages, free)

| Setting | Value |
|---|---|
| Root directory | `web` |
| Build command | `npm ci && npm run build` |
| Output directory | `dist` |
| Environment variable | `VITE_API_URL=https://<api host>` |

`public/_redirects` sends `/sources` to `/connect` and every other path to `index.html`
(client-side routes). `public/_headers` sets security headers (a Content-Security-Policy,
HSTS) and long caching for hashed assets; the build writes `dist/_headers` with the origin of
`VITE_API_URL` in the policy's `connect-src`, and `vite preview` (the e2e server) serves
the same headers, so Playwright runs every screen under the production policy. Set the API's
`WEB_ORIGIN` to the Pages URL so CORS and the Google/Composio redirects come back here.

## How it fits together

```
src/
  api/        openapi-fetch client, auth (tokens), live-update polling, React Query hooks, generated types
  app/        session (user, workspace, role), top row (capsules and menus), Jump to, toast
  lib/        formatting, router, theme (Light/Dark/System), motion (the one animation loop)
  live/       the live sequence: what a poll plays, map effects, count-ups, list motion, live dot
  map/        the one MapLibre map (globe or flat), layers as components, camera, sea lanes,
              placing features, static lookups
  screens/    SignIn, Empty, Connect, Confirm, Main (Overview and the Risks list), RiskView, Scenario
  ui/         icons (copied from System-Icons), shared parts, the score scale, risk rows
  styles.css  tokens (light and night) and parts; map/map.css and live/live.css beside their code
```

- **Auth** (contract decision 6). The access token lives only in memory. The refresh token
  is the one value in `localStorage` (it must survive a reload; there are no cookies, as
  the API is on another site). It is rotated on every use, and refreshes are serialised
  across tabs with the Web Locks API so two tabs never present the same token (which the
  API treats as reuse and revokes the family). A request that gets a 401 refreshes once
  and retries once. A rejected refresh signs out; a network failure keeps the session.
- **Workspace.** Every workspace endpoint gets `X-Workspace-Id`. Query keys start with the
  workspace id, so switching workspace never shows another workspace's rows.
- **Live updates.** The API is serverless, so there is no stream: `LiveStream`
  (`api/stream.ts`) polls `GET /v1/events?after=<id>` about every 15 s, **only while the
  tab is visible** (Page Visibility API). A hidden tab stops at once, so an unwatched
  dashboard never keeps the database awake; when it is shown again it polls right away
  and catches up from its cursor (pages while the API says `more`). `resync` (a cursor
  older than the API's 24 h retention) refetches the workspace's queries. Errors back off
  1 s → 30 s with jitter. Each event invalidates only what it changes
  (`applyStreamEvent` in `api/queries.ts`).
- **Live motion** (`live/`). Each poll's events go to `LivePlayer`. A risk that is new, or
  whose score rose, plays in order: a ripple where its evidence is, a caption, a line along
  the great circle to each entity the story reaches, a pulse there, and only then the list
  update (count-ups, rows sliding with FLIP, a flash on the changed row). At most 3 stories
  per poll, highest first, 400 ms apart; the rest apply at once with only the count-up.
  Nothing plays on the first poll, after a resync, on the poll that catches up after a
  hidden tab, or under reduced motion: the state is applied at once.
- **Theme.** Light, Dark or System from the account menu, kept in `localStorage`
  (`rp.theme`) and applied before first paint by `public/theme-init.js` (a file, as the CSP
  allows no inline script). Every colour is a token on `:root`; the dark set is a night
  map, not an inversion. The map repaints in place (paint properties and sky) on a change.
- **Motion.** One `requestAnimationFrame` loop (`lib/motion.ts`) drives feature-state
  tweens, the sea-lane dash flow, idle rotation and travelling lines; it stops while the
  tab is hidden. Reduced motion turns off rotation, flow, ripples, lines, pulses, the
  breathing dot and View Transitions, and makes flights jumps.
- **Roles.** Viewers see everything but no editing controls; editors confirm mappings,
  change risk status and run what-ifs; only admins connect or disconnect tools (the API
  enforces all of this too).

## The map

No tiles, no keys, no glyph server. The base map is Natural Earth (public domain) served
from `public/map/`, on a globe by default (Globe / Flat on the map controls, remembered in
`rp.projection`, switched with a 200 ms crossfade from a snapshot of the last frame). The
layers are added in bands (`map/slots.ts`), bottom to top: sea, graticule, land, borders,
coast; the tint of countries with open stories; sea lanes; the risk heatmap; entity dots by
score (Low grey, Medium and up coloured), with feature-state tweens for hover, selection
and dimming; labels; short-lived effects.

Each story on screen (the Overview's three rows, the Risks list's first five, and a hovered
or selected one) shows a glass pill at the place the trouble is (`map/StoryPills.tsx`): at the
chokepoint for a chokepoint match ("34 Bab el-Mandeb", whose plain label then hides), else at
the entity it reaches, by its city ("30 Eindhoven"). The ports and route ends a story reaches
only through a chokepoint get no pill; a pill keeps clear of the other pills and of the
chokepoint and site labels showing. These, sites, chokepoints and the live effects are HTML
markers that hide when
the globe turns them away; everything else (city labels, cluster counts on Connect and
Confirm) is MapLibre symbols drawn from the self-hosted Inter with TinySDF, placed only
where they stay inside the open map (`map/labels.ts`).

**Sea lanes** are drawn only for declared routes: the shortest path through a simplified
Eurostat MARNET network (`public/map/lanes.json`, EUPL-1.2) from the route's first port,
through its declared chokepoints in order, to its last port. Shared stretches are drawn
once, with a dash flow toward the destination; the selected route is drawn on top.
Chokepoints under watch say how many drawn lanes pass them. `npm run map-data -- --lanes-only`
rebuilds the network and fails if a chokepoint's gate lies outside its radius, is not on a
lane through it, or has an edge over 1,000 km, or if any edge is over 4,000 km (a segment
across ±180° read as flat lon/lat; the longest real one is 3,611 km).

**Site labels** make way for each other: where two would overlap, the more critical site
(then the first by name) keeps its label and the other hides until the map zooms in far
enough to part them. The squares always show.

Features without a position are placed as the contract says: by their first port (World
Port Index UN/LOCODE) or first country (Natural Earth label point). `/v1/map/risks` places
of type `country`/`port` are joined on `place_key` the same way.

| File | Source | Raw | gzip |
|---|---|---:|---:|
| `countries.geojson` | Natural Earth 1:50m admin-0, simplified to 12 %, 0.01° | 217 KiB | 70 KiB |
| `ports.json` | NGA World Port Index (Pub 150), 3,303 ports by UN/LOCODE, with the standard codes of the WSC Top 50 container ports the WPI files under its own codes | 137 KiB | 62 KiB |
| `centroids.json` | Natural Earth label points and names, 246 codes | 8 KiB | 4 KiB |
| `lanes.json` | Eurostat SeaRoute MARNET (100 km), simplified: 4,009 nodes, 6,176 edges | 56 KiB | 22 KiB |

The 1:50m set is used (not 1:110m) because 1:110m has no Singapore, Hong Kong, Bahrain or
Malta. `ports.json` and `centroids.json` load once, in parallel with MapLibre.

## Bundle

From `npm run check:bundle` on the production build:

| Chunk | Raw | gzip | When |
|---|---:|---:|---|
| App (React, React Query, openapi-fetch, screens) | 438 KiB | 139 KiB | first load |
| MapLibre GL JS | 1,030 KiB | 278 KiB | loaded in parallel, after the shell paints; cached separately |
| CSS (app + MapLibre) | 113 KiB | 19 KiB | first load |
| Inter (variable, optical sizes), Latin subset | 71 KiB | 71 KiB | first load; other subsets only if the page needs them |
| Map data (`public/map/`) | 433 KiB | 162 KiB | first map |

A first visit downloads about 600 KiB compressed, more than half of it MapLibre.

## Tests

- **Vitest + Testing Library + MSW** (`tests/unit/`): auth (memory-only access token,
  single rotation for concurrent callers, reuse → signed out, retry after 401, headers),
  the stream (ticket per connection, reconnect with a new ticket, refetch after
  reconnect), placement, formatting, and each screen against the fixtures, including
  their empty and error states.
- **Playwright** (`tests/e2e/`): every screen at 1360×900, 900 and 375 wide, and at 1360
  and 375 again in the dark theme (`desktop-dark`, `phone-dark`), against the same MSW
  handlers routed with `@msw/playwright`, with reduced motion so the pictures are stable.
  CI uploads them as the `web-screenshots` artifact. `live.spec.ts` turns motion on and
  checks the live sequence for a mocked `risk.created`. WebGL runs on SwiftShader.
- Fixtures (`tests/fixtures/`) describe an invented company. They are never imported by
  `src/`, and `check:bundle` fails if any of their names reach `dist/`.

## Where it differs from the designs, and why

The designs' content is illustration. The structure, tokens, type, spacing, capsules,
breakpoints and wording rules are followed as drawn. These are the deliberate differences.

**Everywhere**
- *Sign in and sign up* are not designed. They reuse the Empty screen's centred glass card
  with the System-Controls fields.
- *Account menu* has the user, unread alerts (with "Mark all read") and Sign out. There is
  no profile page, because none is designed and the API has only name and password changes.
- *Workspace menu* switches and creates workspaces. "Workspace settings" and "Members and
  access" are left out until they are designed.
- *Map:* MapLibre over Natural Earth instead of the rendered image. Routes are not drawn as
  sea lanes: the API has no route geometry, and great-circle lines between ports cross
  land, so a route appears at its ports. Callouts have no leader lines: they sit beside
  their point, flip side to stay on the map, and hide when there is no room (the same risks
  are in the sheet's list, which is also the keyboard path).
- *Bottom sheet on phones:* the grab handle lowers and raises the sheet (tap), rather than
  dragging.
- *Feed health:* the legend's dot is green when every feed is live and amber when one is
  down; hovering or focusing it names the feeds that are down.

**Empty**
- No "Upload a file" button: the API has no upload endpoint, and the view keeps one primary
  button. The step 1 hint lists the tools the server has set up (`/connections/providers`),
  not "Sheets, Excel, accounting, a CRM, or a file".
- When a picked sheet is waiting for review, step 2 is active and the button goes to Confirm.
- The footnote says "No row becomes part of your map until you confirm how we read it",
  because creating a source does store headers and five sample values.

**Connect**
- The tiles are the API's providers (today Google Sheets and Airtable), not a fixed list of
  ten. A tool the server lacks credentials for shows "Not set up on this server".
- "Changes picked up" shows the last edit and last read times of the connection's sources.
  The API has no per-row change log.
- Disconnecting is behind a confirmation dialog with a destructive button, per
  System-Controls.

**Confirm**
- The tab segments are the sources picked from the same spreadsheet. The Picker adds the
  first tab; other tabs need their gid, which the API cannot list yet.
- Field choices come from `mapping.fields`, filtered to the chosen entity kind. "Every row
  is a…" is added, because `MappingSpec.entity_kind` needs it.
- The corner card says how many entities the preview placed and how many countries and
  ports will be watched. "5 risks already touch you" is not shown: the preview endpoint
  returns entities and problems, not risks. Problem rows are listed instead.
- A valid UN/LOCODE that `ports.json` does not have gets no map position and no lane, so the
  card names it: "2 port codes we cannot place: XXABC, YYDEF." (six named, then "+N").

The risk cards were redesigned for plain reading (approved on the owner's design canvas,
27 Nov 2026). One element carries the system: the **score scale**, 0–100 in four bands sized
to the levels (Low 0–39, Medium 40–59, High 60–79, Critical 80–100: 4 : 2 : 2 : 2 with 2 px
gaps), a dot marking the score (`ui/ScoreScale.tsx`): large on the risk page, mini under each
row's number, as ticks on the Overview, and a 48 × 4 copy in the map legend ("Score out of
100"). Every row is one **story**, a category at a place (`Risk.story`), in three lines: a
place phrase ("Shipping through Bab el-Mandeb", `placePhrase` in `lib/format.ts`), the real
headline of its strongest report (`Risk.lead`), and what it reaches ("8 routes and 7
suppliers ship through it", `lib/story.ts`).

**Main (Overview)**
- A sentence title ("18 risks touch your business"), then the levels ("All are Low", "2 are
  High, 16 Low") and when the scores were last checked, beside the live dot.
- Every open story as a tick on the scale (`RiskSummary.story_scores`), the highest written
  above it. The API has no portfolio index history, so there is no 30-day sparkline.
- The top three stories; the selected one opens in place with the receipt (Likely, Impact,
  each with its reason) and "Open risk" / "What if".
- The legend says "N of M feeds live" from feed health, or names a broken source with Fix.

**Risks** (the tab, at `/register` and `/risks`; `/overview` is `/`)
- The phone design's list, used at every width: "Highest first" / "Newest first" in a menu,
  category chips with counts on one scrolling row, and "Show more" paging.

**Risk**
- No "R-011" style reference: risks have UUIDs only.
- The place phrase; one sentence on why it reaches you (from the story's entities, their
  kind and criticality and the place, else the engine's explanation); the score with its
  level and its move ("Up 3 since yesterday": against the latest history point at least 24 h
  old); the scale; and the receipt: Likely (what the reports have in common: "4 reports in 2
  days name the Netherlands") and Impact (as a percentage, so likely × impact = score, with
  the category and the criticality or share of spend).
- The news: two reports of the latest scoring, "All N" for the rest. Then "What to do": the
  advice text as plain steps. There is no "in your plan" checklist, because the API cannot
  store a plan. "Assign" is left out, as risks have no owner field.
- The formula ("How this was scored") is one tap away: "···" → Show the working.
- Status changes (acknowledge, mitigating, dismiss, reopen) use `PATCH /v1/risks/{id}`,
  with Undo in the toast.

**Scenario**
- The four choices are the shocks the API takes: a choke point closing, an event in a
  country, a currency move, and a sanctioned entity. "How long it lasts" (weeks) is replaced
  by severity, or by the size of the move for currencies, because the engine has no
  duration input.
- Results are the engine's: risk index (portfolio score) before → after, risk count and
  highest score, and the largest changes. There is no cost figure, because the engine
  returns none.
- "Save" and "Add to risk plan" are gone: every run is stored, and recent runs are listed
  under "Saved what-ifs". The view's one primary button is "Run what-if".

## Contract

The redesign added `Risk.where`, `Risk.lead`, `story.entities[].kind/city/criticality`,
`story.entity_count`, `story.kind_counts` and `RiskSummary.story_scores`, and the API now
groups stories by category and place (additive: every earlier field is kept). The gaps above
(portfolio index history, risks in the mapping preview, sheet tabs, risk owners and plans,
scenario duration and cost) are candidates for later contract changes.
