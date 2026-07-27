# Timeline Diagrams

Timeline diagrams present a chronological list of events grouped into optional
sections. They are the right choice when the *order* and *grouping* of events
matters but exact durations do not — incident reports, attack narratives,
product history, release chronologies, onboarding sequences.

Use a **Gantt chart** instead when you need real durations, dependencies, or
calendar-accurate bars. Use a **sequence diagram** when the story is about
messages between participants rather than points in time.

## Basic Syntax

```mermaid
timeline
    title History of Social Media Platform
    2002 : LinkedIn
    2004 : Facebook
    2005 : YouTube
    2006 : Twitter
```

**Structure:**
- First line is `timeline`
- Optional `title <text>`
- Each event line is `<time period> : <event>`
- Everything before the first `:` is the time period; it is free text
- Everything after is the event text

## Multiple Events per Period

Chain additional events with more colons, or put each on its own indented line:

```mermaid
timeline
    title Platform Launches
    2004 : Facebook : Flickr
    2005 : YouTube
         : Reddit
```

Both forms render as several event boxes stacked under the same period.

## Sections

Sections group consecutive periods under a heading and give each group its own
color:

```mermaid
timeline
    title Product History
    section Founding
        2019 : Company incorporated
        2020 : First prototype
    section Growth
        2021 : Public beta : Seed round
        2022 : GA release
```

Every period after a `section` line belongs to that section until the next
`section` line.

## Free-Text Periods

The period column is not parsed as a date, so any label works — clock times,
ranges, phase names, version numbers:

```mermaid
timeline
    title Deploy Window
    section Rollout
        09h00 : Canary to 5%
        09h00 to 11h00 : Ramp to 50%
        Post-deploy : Error budget unchanged
```

## Worked Example: Incident / Attack Timeline

A carding-attack narrative, where the interesting structure is phase grouping
plus a few hard timestamps and some window-wide aggregates:

```mermaid
timeline
    title Seller 118519 Carding Timeline - 2026-07-16 UTC

    section Ramp-up
        12h30 : First card-hash block observed
        13h00 : Window start, attack via whitelisted IPs

    section Peak and auto-defense
        13h00 to 16h00 : Bulk of 1697 attempts, including 1340 generic declines
        16h25 : Automatic block-list additions begin, source 1
        16h35 : Issuer-sourced block, source 4
        17h58 : Last of 69 in-window blocks

    section Containment
        13h00 to 24h00 : 455 attempts rejected before gateway
        Full window : IP blocks 0, rate-limit blocks 0
```

**Why this works:**
- Sections carry the analytical story (ramp-up → peak → containment), not just time
- Exact timestamps are used where a discrete thing happened; ranges where a
  volume accumulated
- Counts live in the event text, so the diagram doubles as the summary table
- A final `Full window` period holds window-wide facts, including the negative
  findings (zero IP blocks, zero rate-limit blocks) that rule causes out

## Styling

Timelines accept the standard themes plus a `disableMulticolor` option that
turns off per-section coloring:

```mermaid
---
config:
  theme: base
  timeline:
    disableMulticolor: true
---
timeline
    title Monochrome Timeline
    section Phase 1
        Step A : Did the thing
```

Custom section colors come from `themeVariables` `cScale0`, `cScale1`, … (one
per section, in order):

```mermaid
---
config:
  themeVariables:
    cScale0: "#2c6e49"
    cScale1: "#d68c45"
    cScale2: "#8c2f39"
---
timeline
    section Green
        A : first
    section Orange
        B : second
    section Red
        C : third
```

## Pitfalls

- **Colons in event text** split the line into more events — write `12h30 UTC`
  rather than `12:30 UTC`, or the time becomes two boxes
- **No date math** — periods are strings, so entries render in the order you
  write them; the diagram will not sort or space them chronologically
- **Long event text** widens boxes and squeezes neighbors; keep events to a
  short clause and push detail into surrounding prose
- **Sections are sequential** — you cannot reopen an earlier section later in
  the diagram; reorder the events instead
- **Renderer support** — timeline needs Mermaid v9.4+; older embeds fall back to
  a syntax error
