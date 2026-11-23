# Design System: Recruence

The single source of truth for generating new screens (Google Stitch or by hand) that match the shipped dashboard (`app/web/styles.css`). Values in parentheses are the exact tokens in that file.

## 1. Visual Theme & Atmosphere

A calm, light-blue glass workspace for recruiters running structured AI interviews. Frosted white panels float on a pale sky-blue field with slow-drifting, blurred blue light behind them, like daylight through frosted glass. It should feel trustworthy and quiet, never playful, because the work is someone's job application.

- **Density 5, "Daily App Balanced":** dashboards, tables and long reports, with generous padding inside panels and clear gaps between them.
- **Variance 4, "Offset, not chaotic":** left-aligned page headers with actions pushed to the right; asymmetric splits for detail pages (content plus a narrower side column). No centred heroes: this is a working tool, not a landing page.
- **Motion 4, "Fluid CSS":** short, soft transitions and a few slow ambient loops. Nothing bounces or demands attention.

The whole app sits inside **one rounded glass frame**: the sidebar and the workspace share it, split by a single hairline, so navigation and content read as one surface.

## 2. Color Palette & Roles

One accent hue (blue) in a few steps, on cool blue-white neutrals. Status colours appear only to mean status.

**Field and surfaces**
- **Morning Sky** (#EAF4FF) — page background, top of the field.
- **Deeper Sky** (#DCEEFF) — page background, bottom of the field.
- **Ambient Glow** (#B9DEFF) and **Horizon Blue** (#8AC5F9) — only in the blurred background light; never on components.
- **Frosted Glass** (rgba(255,255,255,0.48)) — every panel, card, dialog and the app frame's contents.
- **Soft Glass** (rgba(255,255,255,0.28)) — the app frame itself (rgba(255,255,255,0.30)) and recessed areas.
- **Strong Glass** (rgba(255,255,255,0.72)) — dialogs, dropdowns and anything that must stay readable over content.
- **Glass Edge** (rgba(255,255,255,0.65)) — the 1px border on every glass surface.
- **Blue Hairline** (rgba(52,127,234,0.15)) — dividers, table rules, input borders, the sidebar's section lines.

**Accent (one hue, four steps)**
- **Signal Blue** (#347FEA) — primary accent: focus rings, chart lines, list markers. Too light for small text (3.6:1).
- **Label Blue** (#1F5AD6) — the same hue, deepened for small uppercase labels (eyebrows, sidebar section labels, form section titles): 5.1–6:1 on every background.
- **Electric Blue** (#2563EB) — links, active text, the deep end of the primary gradient.
- **Soft Blue** (#559BEE) — secondary fills and chart areas.
- **Glacier Cyan** (#64C8F5) — the light end of bar and score gradients only.
- **Primary Gradient** (135deg, #3E8BF0 → #2563EB) — primary buttons, the active navigation tile, selected options. The only gradient allowed on components.

**Ink**
- **Deep Harbour** (#17324F) — primary text. Never pure black.
- **Slate Current** (#526B85) — secondary text, descriptions, metadata.
- **Mist** (#5A738C) — placeholders, notes, axis labels, table headers: the lightest text allowed (4.6:1 on glass panels, WCAG AA).

**Status (meaning only, always with a text label)**
- **Verdant** (#16A979, text #0B7655, fill rgba(22,169,121,0.12)) — ready, completed, meets, advance, connected.
- **Amber** (#E9A23B, text #8F5A0B, fill rgba(233,162,59,0.15)) — scheduled, in progress, not enough evidence, hold, warnings.
- **Coral** (#E45F70, text #B4283B, fill rgba(228,95,112,0.12)) — failed, does not meet, reject, destructive actions, errors.

**Transcript roles**
- AI interviewer: Signal Blue at 10% (rgba(52,127,234,0.10)), left-aligned bubble.
- Candidate: white glass (rgba(255,255,255,0.78)), right-aligned bubble.
- System: Amber at 12% (rgba(233,162,59,0.12)), centred, smaller text.

## 3. Typography Rules

- **Typeface:** the shipped stack is Inter → Plus Jakarta Sans → Segoe UI Variable → system UI, with no web fonts downloaded (self-hosted, private installs). **For Stitch mockups use Plus Jakarta Sans** throughout, for display and body alike, so mockups avoid Inter and stay sans-serif.
- **Mono:** ui-monospace / Cascadia Code / Consolas, only for environment-variable names and ids.
- **Page title:** 1.5–2rem (clamp), weight 760, tracking −0.025em, Deep Harbour. Above it a small **eyebrow**: 0.74rem, weight 700, uppercase, tracking 0.08em, Label Blue ("DASHBOARD", "EVALUATION REPORT").
- **Section titles:** 1.05rem, weight 700, preceded by a 32px rounded icon tile tinted Signal Blue at 10%.
- **Body:** 15px, line height 1.55; descriptions in Slate Current at 0.84–0.9rem; prose capped near 70 characters per line.
- **Numbers:** tabular figures for scores, counts and timers. Stat numbers 2.4rem, weight 780, tight tracking.
- **Labels:** sentence case ("Total interviews", "AI recommendation"). Uppercase only for eyebrows and section labels in the sidebar.
- Hierarchy comes from weight and colour first, size second. No gradient text.

## 4. Component Stylings

- **App frame:** one container, max 1480px wide, 32px corners, Soft Glass, large diffused blue-tinted shadow. Sidebar (256px) on the left, separated from the workspace by a single Glass Edge hairline. Below 768px the frame dissolves and the sidebar becomes a slide-out drawer.
- **Sidebar:** a typographic wordmark ("Recruence", 700 weight, -0.022em tracking; no logo, no tagline), then the Workspace navigation under a Blue Hairline, and a collapse control at the foot. Expanded 256px; collapsed 76px with icons only, the wordmark shortened to "R" (a first word of up to four letters is kept whole) and names in a dark tooltip. The choice is remembered. Navigation rows are pills: a 34px round icon tile tinted blue, then the label. The **active page** is a raised white pill whose icon tile fills with the Primary Gradient and turns its icon white.
- **Top bar:** a pill-shaped global search (46px tall, search icon inside on the left) over candidates and roles, with a grouped results dropdown on Strong Glass. The signed-in user sits on the right as a pill chip: round initials avatar, name, role.
- **Panels / cards:** Frosted Glass, Glass Edge border, 22px corners (28px for the largest), a soft blue-tinted shadow plus a 1px inner top highlight. A card exists only to group related content; plain lists inside panels use hairline dividers, not nested cards.
- **Stat cards:** label and small icon tile on top; big number with a one-line note beneath; a small 14-day area trend line to the right of the number (stroke in the card's status colour, fill fading to transparent).
- **Buttons:** fully rounded pills. **Primary:** Primary Gradient fill, white text, soft blue shadow. **Secondary:** translucent white with Glass Edge border; on hover it brightens, its border turns blue and it lifts 1px. **Danger:** Coral text on a 8% Coral tint, solid Coral on hover. Decision buttons use Verdant / Amber / Coral tints. Active state settles back to 0 lift. Loading shows a small inline ring plus a verb ("Saving…"). No outer glow, no custom cursors.
- **Inputs:** label above (0.84rem, weight 600), optional hint beside it in Slate Current, error below in a Coral notice. Fields are 66% white with a Blue Hairline border and 12px corners; focus turns the field almost opaque with a 3px Signal Blue ring. Selects use a custom chevron. No floating labels.
- **Pills and badges:** small fully rounded tags with a 6px leading dot in the status colour, status-tinted fill and darker status text. Live states (joining, in progress, evaluating) pulse their dot.
- **Reduced transparency:** when the system asks for less transparency, every glass surface becomes solid (white panels, a pale blue frame) and the background light is hidden.
- **Notices:** 12px corners, tinted fill and border in the status colour, ink-coloured text. Info notices use Signal Blue at 10%.
- **Dialogs:** Strong Glass at 82%, 28px corners, blurred dimmed backdrop. A round icon tile in the title (Coral for destructive). Cancel on the left, the action on the right; destructive dialogs focus Cancel first. Replace every browser `alert` / `confirm`.
- **Toasts:** bottom-right, Strong Glass, a coloured status dot, dismiss "×", auto-hide after a few seconds.
- **Tables:** no outer borders; header row in small uppercase Mist; rows separated by faint Blue Hairlines; clickable rows highlight white on hover and open with Enter.
- **Score ring (report):** a conic ring in Electric Blue on a 12% blue track, white centre with the average score.
- **Empty states:** a 56px rounded icon tile, one bold line saying what is missing, one muted line on how to fill it, and the action button(s).
- **Loading:** pages show "Loading…" with a small ring only if they take longer than 250ms; avoid full-screen spinners.
- **Logo:** not used in the dashboard (the wordmark is the brand). The robot interviewer tile (`app/web/logo.png`) remains for the favicon and the candidate's booking page.
- **Account menu:** top right, a pill with a round initial, the name and a chevron; it opens a white glass menu with who is signed in, then Settings, Access and sign-in, Sign out (only what works for that session).

## 5. Layout Principles

- Workspace content max 1240px, padded 26–32px; panels separated by 18px.
- **Page header:** eyebrow, title and one-line description on the left; actions aligned right on the same baseline.
- **Detail pages:** a main column plus a 340px side column (live interview: transcript + stats/notes). The side column stays visible while scrolling on desktop.
- **Equal-width rows are for peer data only:** four stat cards, three meeting-platform choices, three report summaries (strengths, areas, next round). Never decorative "three feature cards".
- CSS Grid with `minmax(0, 1fr)` columns; every grid can shrink without overflowing.
- Spacing scale in practice: 6, 8, 10, 12, 14, 16, 18, 20, 26, 32px.
- Nothing overlaps: every element has its own space; dropdowns and dialogs float above, they never cover content permanently.

## 6. Responsive Rules

- **≤1100px:** stat cards 2 per row; the dashboard's columns stack.
- **768-1199px:** the sidebar is always compact (76px, icons with tooltips), with no collapse control.
- **≤960px:** two-column detail pages become one column.
- **≤767px:** the frame dissolves to full screen. The top bar becomes a sticky glass pill holding the menu button, the wordmark and the account button, with search on a second row. The sidebar becomes a slide-out drawer over a dimmed, blurred page; Escape or a tap outside closes it. Two-column detail pages become one column.
- **≤640px:** single column everywhere; less important table columns hide; panels pad 16px.
- No horizontal page scroll at any width (wide tables scroll inside their panel).
- Tap targets at least 40–44px; the search field is 42–46px tall.

## 7. Motion & Interaction

- One easing curve for everything (cubic-bezier(0.2, 0.7, 0.2, 1)); 150–280ms.
- Hover: a 1px lift with a slightly stronger shadow. Press: settle back.
- Ambient: four blurred blue orbs drift slowly (26–38s, alternate) behind the frame; they never react to the pointer.
- Live: status dots pulse; new transcript lines rise 6px and fade in; a jumped-to transcript line flashes a blue ring once.
- Dialogs and toasts pop in (8px rise, 0.98 → 1 scale, 200ms).
- Animate only `transform` and `opacity` (plus colour and shadow on hover).
- `prefers-reduced-motion`: all animation and transition durations drop to near zero and the orbs stop.

## 8. Anti-Patterns (Never Do)

- No invented data: no placeholder people, fake metrics or round "99.9%" figures. Every number on screen comes from real interviews.
- No emojis.
- No pure black (#000000) and no grey that isn't part of the cool blue-grey ink scale.
- No neon, outer glows or saturated glow shadows; shadows are soft and tinted blue.
- No second accent hue (no purple, no teal brand colour). Green, amber and coral mean status only.
- No gradient text, and no gradient on anything except the primary action, the active selection and chart fills.
- No serif fonts.
- No decorative three-card feature rows, and no centred marketing hero.
- No hard dark borders; edges are glass highlights and blue hairlines.
- No browser `alert` / `confirm` dialogs.
- No em dashes or en dashes in interface text: plain hyphens for ranges ("2-3 minutes", "1-5"), and words for empty values ("Not evaluated", "Undecided", "None yet"), not a dash.
- Text below 4.5:1 contrast against its background.
- No copywriting clichés ("elevate", "seamless", "unleash", "next-gen"). Plain, specific sentences: say what happens and what the recruiter can do.
- No filler UI ("scroll to explore", bouncing chevrons).
- Colour is never the only signal: every status pill, verdict and notice carries words.
- Never hide that the AI is advisory: reports always carry the human-decision disclaimer.
