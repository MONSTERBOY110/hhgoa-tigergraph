---
name: Kavach
description: Agentic fraud investigation on TigerGraph, shown as an auditable case ledger.
colors:
  tg-orange: "#f58220"
  tg-soft: "rgba(245,130,32,.14)"
  tg-line: "rgba(245,130,32,.55)"
  ground: "#15171c"
  panel: "#1c1f26"
  panel-2: "#222630"
  line: "#2d323d"
  line-2: "#3a404d"
  ink: "#ecebe8"
  ink-2: "#b7bcc6"
  ink-3: "#8d94a1"
  fraud: "#ff6b5b"
  legit: "#56d39c"
  uncertain: "#f2c14e"
typography:
  display:
    fontFamily: "JetBrains Mono, ui-monospace, Consolas, monospace"
    fontSize: "28px"
    fontWeight: 600
    letterSpacing: "-.01em"
  headline:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.2
  title:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 650
    letterSpacing: ".01em"
  body:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum"
  body-small:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "12.5px"
    fontWeight: 400
  label:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 400
    letterSpacing: ".07em"
  mono-id:
    fontFamily: "JetBrains Mono, ui-monospace, Consolas, monospace"
    fontSize: "11.5px"
    fontWeight: 400
rounded:
  chip: "4px"
  base: "6px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "10px"
  md: "16px"
  lg: "22px"
  xl: "34px"
components:
  panel:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.base}"
    padding: "16px 18px"
  filter-chip:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  filter-chip-active:
    backgroundColor: "{colors.tg-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
  tab:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.base}"
    padding: "8px 12px"
  tab-hover:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.ink}"
  id-chip:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.ink-2}"
    typography: "{typography.mono-id}"
    rounded: "{rounded.chip}"
    padding: "1px 6px"
  gauge:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.headline}"
    padding: "14px 16px 12px"
---

# Design System: Kavach

## Overview

**Creative North Star: "The Night Ledger"**

A fraud case read the way an auditor checks it: evidence, then the rule it triggers, then the action and who must approve it. The world is TigerGraph orange on a cool charcoal ground, dense and quiet, with a single six-instrument readout strip per case and an ego-graph drawn in orange edges. It rejects the metric-hero dashboard and the card grid; every surface is a ruled panel or a ledger table.

Density is working-tool density (14px body, 12.5px secondary), with depth made from tonal steps and 1px hairlines rather than shadows. Monospace is reserved for things a machine emits: IDs, actions, routes, fingerprints.

**Key Characteristics:**
- Dark only; three tonal surfaces (ground, panel, panel-2) separated by hairlines.
- One brand accent (orange) for selection, focus, links and graph edges that carry evidence.
- Three verdict colors, used as small dots and outlines, never as fills behind text.
- Instruments own one truth each: verdict, probability, pattern, exposure, SAR, graph write-back.

## Colors

Cool charcoal neutrals, one warm brand accent, and a three-state semantic set shared by verdicts and approval routes.

### Primary
- **TigerGraph Orange** (tg-orange): focus rings, active tab underline, links, selection, evidence-bearing graph edges and nodes, the evidence-request arrow. `tg-soft` fills the active filter chip; `tg-line` is its border and the stroke of "hot" graph edges.

### Neutral
- **Night Ground** (ground): page and rail background, and the dark text on orange selection.
- **Ledger Panel** (panel): top bar, panels, instrument strip, inactive chips.
- **Raised Panel** (panel-2): selected case row, tab hover, evidence bridge column, ID chips.
- **Hairline** (line) and **Strong Hairline** (line-2): every border and table rule; line-2 also for scale track, scrollbar thumb and selected-row inset ring.
- **Ink** (ink), **Ink Secondary** (ink-2), **Ink Tertiary** (ink-3): primary text, supporting text, placards and metadata.

### Semantic
- **Fraud Coral** (fraud): fraud verdict, L2 route, confirmed-fraud similar cases, the high zone of the probability scale (0.85 to 1).
- **Legit Mint** (legit): legitimate verdict, cleared similar cases, the low zone of the scale (0 to 0.15), customer evidence icon.
- **Uncertain Amber** (uncertain): uncertain verdict, L1 route, external evidence icon.
- The auto route uses ink-3.

### Named Rules
**The One Accent Rule.** Orange means "this is the thread to follow" (focus, selection, evidence). It is never a verdict color and never a large fill.

**The Shared Semantics Rule.** Coral, mint and amber carry the same meaning everywhere: coral is the severe end (fraud, L2), amber the middle (uncertain, L1), mint the safe end (legitimate, cleared).

## Typography

**Display Font:** JetBrains Mono (with ui-monospace, Consolas)
**Body Font:** Manrope (with Segoe UI, system-ui)

**Character:** A plain geometric sans for reading, a mono for identifiers; the case id is the page's only display type, set in mono because it is an ID. Tabular numerals are on globally so amounts align.

### Hierarchy
- **Display** (mono 600, 28px, -.01em): the case id heading.
- **Headline** (650, 20px, 1.2): instrument values in the readout strip; 16px when the value wraps, 15px mono for the graph case id.
- **Title** (650, 15px): section headings, with a 13px ink-3 aside on the same line. Column heads inside panels are 13px 650.
- **Body** (400, 14px, 1.5): summaries and prose, capped near 75ch; SAR narrative at 1.65 line height, 78ch.
- **Body small** (400, 12.5px): action reasons, metadata, footline, legend (11.5px).
- **Label** (400, 11px, .07em, uppercase): instrument placards under each value. Table column heads use the same treatment at 11.5px, 600, .06em.
- **Mono ID** (400, 11.5px to 13px): entity chips, refs, case ids in the rail, action ids (12.5px 600), routes (11px 600).

### Named Rules
**The Machine Text Rule.** Mono is only for strings the system emits (IDs, action enums, routes, refs). Human prose is always Manrope. A literal pipe inside Manrope prose is re-set in mono so it does not read as a capital I.

## Layout

App shell: a 56px top bar, then a two-column grid with a 320px case rail and a fluid main column; the case article is centered at max 1180px with 26px 32px 64px padding. Within a case: header, trigger line, the six-cell instrument strip (column weights 1.4 / 1.6 / 1.5 / 1 / 1 / 1.2), then graph beside summary (1.05fr / 1fr, 22px gap), then evidence ledger, next best actions, SAR, footline. Sections are separated by 34px.

Actions sit in three columns: initial, a 190px bridge showing the evidence request, final.

Responsive: at 1100px the strip becomes 3 by 2 and graph and summary stack. At 820px the shell becomes one column (rail capped at 46vh above content), gutters drop to 16px, the bridge turns horizontal with a down arrow, bar metadata hides, and ledger rows become stacked blocks. At 520px the strip is 2 columns.

## Elevation & Depth

Flat. There are no drop shadows. Depth comes from the three tonal surfaces and 1px hairlines; state is shown with inset rings (`inset 0 0 0 1px` line-2 on the selected row) and an inset 2px orange underline on the active tab.

### Named Rules
**The Hairline Rule.** Separate with a 1px line or a tonal step, never a shadow. Instrument cells and ledger rows are divided by hairlines inside one bordered panel, not boxed individually.

## Shapes

Gently rounded (6px) for panels, tabs, rows and the strip; 4px for ID chips and route badges; full pills for filter chips and the case status. Graph node vocabulary is fixed: circles for cards and transactions (20px radius for the subject card), 12px rounded squares for similar closed cases, a hexagon for a shared device. Similar-case edges are dashed (3 4).

## Components

### Tabs and top bar
Text tabs in ink-2 on the panel bar; hover lifts to panel-2 and ink; the selected tab gets an inset 2px orange bottom rule. Brand is a shield glyph plus the wordmark at 16px 650.

### Filter chips
Pills with a hairline border and a trailing ink-3 count. Pressed state: tg-soft fill, tg-line border, ink text.

### Case rail rows
Three-column grid (74px mono id, verdict dot plus word, right-aligned exposure) with the pattern and trigger on a second, truncated line. Hover to panel; current row to panel-2 with a line-2 inset ring.

### Instrument strip (signature)
Six cells inside one bordered panel, hairline dividers, value above an uppercase placard. The probability cell carries a thresholded scale: 2px track, mint zone below 0.15, coral zone above 0.85, ticks at 0.15, 0.30, 0.70, 0.85, and a 3px ink needle that eases into place (0.5s, cubic-bezier(.16,1,.3,1)).

### Ego-graph (signature)
Subject card at the center with a verdict-colored ring; episode transactions on an upper arc in orange; connected cards on the right via a shared-device hexagon; similar closed cases lower left, dashed, outlined mint or coral. Edges draw in over 0.7s; labels in 10.5px mono are nudged apart to avoid overlap. A legend sits under the graph.

### Evidence ledger
Full-width table: claim with mono ref beneath, source with a 14px stroke icon (graph orange, document ink-2, customer mint, external amber), and a wrap of ID chips capped at 8 plus a "+n" chip.

### Action rows and route badges
Action id in 12.5px mono 600, route badge on the right as an outlined mono tag in its semantic color, reason on a full-width second line in ink-2. Rows divided by hairlines.

## Do's and Don'ts

### Do:
- **Do** keep every surface on ground, panel or panel-2 and divide with line or line-2 hairlines.
- **Do** use orange only for focus (2px outline, 2px offset), selection, links and evidence-bearing graph elements.
- **Do** show verdicts as an 8px to 10px dot plus the verdict word; routes as outlined mono badges.
- **Do** set IDs, action enums and refs in JetBrains Mono, and everything a human reads in Manrope with tabular numerals.
- **Do** honor reduced motion: the edge draw and the needle ease are disabled under `prefers-reduced-motion`.

### Don't:
- **Don't** build metric-hero tiles or card grids; a case is a ledger read top to bottom.
- **Don't** add drop shadows; depth is tonal.
- **Don't** fill large areas with verdict colors or put text on them.
- **Don't** use mono for prose or Manrope for IDs.
