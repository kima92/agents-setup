---
name: mermaid-diagrams
description: Comprehensive guide for creating software diagrams using Mermaid syntax. Use when users need to create, visualize, or document software through diagrams including class diagrams (domain modeling, object-oriented design), sequence diagrams (application flows, API interactions, code execution), flowcharts (processes, algorithms, user journeys), entity relationship diagrams (database schemas), C4 architecture diagrams (system context, containers, components), state diagrams, git graphs, pie charts, gantt charts, timelines (incident chronologies, attack narratives, project/product history), or any other diagram type. Triggers include requests to "diagram", "visualize", "model", "map out", "show the flow", "timeline of events", "chronology", or when explaining system architecture, database design, code structure, incident sequences, or user/application flows.
---

# Mermaid Diagramming

Create professional software diagrams using Mermaid's text-based syntax. Mermaid renders diagrams from simple text definitions, making diagrams version-controllable, easy to update, and maintainable alongside code.

## Core Syntax Structure

All Mermaid diagrams follow this pattern:

```mermaid
diagramType
  definition content
```

**Key principles:**
- First line declares diagram type (e.g., `classDiagram`, `sequenceDiagram`, `flowchart`)
- Use `%%` for comments
- Line breaks and indentation improve readability but aren't required
- Unknown words break diagrams; parameters fail silently

## Diagram Type Selection Guide

**Choose the right diagram type:**

1. **Class Diagrams** - Domain modeling, OOP design, entity relationships
   - Domain-driven design documentation
   - Object-oriented class structures
   - Entity relationships and dependencies

2. **Sequence Diagrams** - Temporal interactions, message flows
   - API request/response flows
   - User authentication flows
   - System component interactions
   - Method call sequences

3. **Flowcharts** - Processes, algorithms, decision trees
   - User journeys and workflows
   - Business processes
   - Algorithm logic
   - Deployment pipelines

4. **Entity Relationship Diagrams (ERD)** - Database schemas
   - Table relationships
   - Data modeling
   - Schema design

5. **C4 Diagrams** - Software architecture at multiple levels
   - System Context (systems and users)
   - Container (applications, databases, services)
   - Component (internal structure)
   - Code (class/interface level)

6. **Timelines** - Chronological events grouped into phases
   - Incident and attack chronologies
   - Product/project history
   - Release and rollout narratives
   - Any "what happened, in what order" story where durations don't matter

7. **State Diagrams** - State machines, lifecycle states
8. **Git Graphs** - Version control branching strategies
9. **Gantt Charts** - Project timelines with real durations and dependencies
10. **Pie/Bar Charts** - Data visualization

## Quick Start Examples

### Class Diagram (Domain Model)
```mermaid
classDiagram
    Title -- Genre
    Title *-- Season
    Title *-- Review
    User --> Review : creates

    class Title {
        +string name
        +int releaseYear
        +play()
    }

    class Genre {
        +string name
        +getTopTitles()
    }
```

### Sequence Diagram (API Flow)
```mermaid
sequenceDiagram
    participant User
    participant API
    participant Database

    User->>API: POST /login
    API->>Database: Query credentials
    Database-->>API: Return user data
    alt Valid credentials
        API-->>User: 200 OK + JWT token
    else Invalid credentials
        API-->>User: 401 Unauthorized
    end
```

### Flowchart (User Journey)
```mermaid
flowchart TD
    Start([User visits site]) --> Auth{Authenticated?}
    Auth -->|No| Login[Show login page]
    Auth -->|Yes| Dashboard[Show dashboard]
    Login --> Creds[Enter credentials]
    Creds --> Validate{Valid?}
    Validate -->|Yes| Dashboard
    Validate -->|No| Error[Show error]
    Error --> Login
```

### ERD (Database Schema)
```mermaid
erDiagram
    USER ||--o{ ORDER : places
    ORDER ||--|{ LINE_ITEM : contains
    PRODUCT ||--o{ LINE_ITEM : includes

    USER {
        int id PK
        string email UK
        string name
        datetime created_at
    }

    ORDER {
        int id PK
        int user_id FK
        decimal total
        datetime created_at
    }
```

### Timeline (Incident Chronology)
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

Periods are free text (`12h30`, `13h00 to 16h00`, `Full window`), so clock
times, ranges, and window-wide aggregates all sit on the same axis. Note the
`12h30` style — a literal `12:30` would split into two event boxes, since `:`
separates period from event.

## Detailed References

For in-depth guidance on specific diagram types, see:

- **[references/class-diagrams.md](references/class-diagrams.md)** - Domain modeling, relationships (association, composition, aggregation, inheritance), multiplicity, methods/properties
- **[references/sequence-diagrams.md](references/sequence-diagrams.md)** - Actors, participants, messages (sync/async), activations, loops, alt/opt/par blocks, notes
- **[references/flowcharts.md](references/flowcharts.md)** - Node shapes, connections, decision logic, subgraphs, styling
- **[references/erd-diagrams.md](references/erd-diagrams.md)** - Entities, relationships, cardinality, keys, attributes
- **[references/c4-diagrams.md](references/c4-diagrams.md)** - System context, container, component diagrams, boundaries
- **[references/architecture-diagrams.md](references/architecture-diagrams.md)** - Cloud services, infrastructure, CI/CD deployments
- **[references/timeline-diagrams.md](references/timeline-diagrams.md)** - Periods, sections, multi-event lines, incident chronologies, section coloring
- **[references/escaping-and-labels.md](references/escaping-and-labels.md)** - Label quoting, which characters to escape (and which not to), node ID rules, reserved words
- **[references/advanced-features.md](references/advanced-features.md)** - Themes, styling, configuration, layout options

## Best Practices

1. **Start Simple** - Begin with core entities/components, add details incrementally
2. **Use Meaningful Names** - Clear labels make diagrams self-documenting
3. **Comment Extensively** - Use `%%` comments to explain complex relationships
4. **Keep Focused** - One diagram per concept; split large diagrams into multiple focused views
5. **Version Control** - Store `.mmd` files alongside code for easy updates
6. **Add Context** - Include titles and notes to explain diagram purpose
7. **Iterate** - Refine diagrams as understanding evolves

## Configuration and Theming

Configure diagrams using frontmatter:

```mermaid
---
config:
  theme: base
  themeVariables:
    primaryColor: "#ff6b6b"
---
flowchart LR
    A --> B
```

**Available themes:** default, forest, dark, neutral, base

**Layout options:**
- `layout: dagre` (default) - Classic balanced layout
- `layout: elk` - Advanced layout for complex diagrams (requires integration)

**Look options:**
- `look: classic` - Traditional Mermaid style
- `look: handDrawn` - Sketch-like appearance

## Exporting and Rendering

**Native support in:**
- GitHub/GitLab - Automatically renders in Markdown
- VS Code - With Markdown Mermaid extension
- Notion, Obsidian, Confluence - Built-in support

**Export options:**
- [Mermaid Live Editor](https://mermaid.live) - Online editor with PNG/SVG export
- Mermaid CLI - `npm install -g @mermaid-js/mermaid-cli` then `mmdc -i input.mmd -o output.png`
- Docker - `docker run --rm -v $(pwd):/data minlag/mermaid-cli -i /data/input.mmd -o /data/output.png`

## Labels and Escaping (read before debugging a broken diagram)

Most render failures are label-quoting failures. The short version — see
**[references/escaping-and-labels.md](references/escaping-and-labels.md)** for the
tested details:

- **Quote every label**: `A["User (admin)"]`. Unquoted `A[User (admin)]` is a
  parse error. Quoting makes parens, braces, pipes, semicolons, and unbalanced
  brackets safe in one move.
- **Escape only three things** inside quotes: `"` → `&quot;` (a raw inner quote is
  *silently dropped*), `<`/`>` → `&lt;`/`&gt;` (raw tags are injected as real
  HTML), and newline → `<br/>`.
- **Don't escape** `&`, `#`, or `|` — they render fine raw inside quotes.
- **Never use `&#NN;`** numeric entities: `&#35;` renders as literal `&#35`.
  Mermaid's own escape is hash-prefixed (`#35;`), but raw `#` already works.
- **Node IDs**: hyphens and digits are fine; spaces break; `end` (lowercase) is a
  reserved word and breaks.

## Common Pitfalls

- **Unquoted labels** - The #1 cause of parse errors; quote anything with punctuation
- **Silent corruption** - Raw `"` and `<` don't error, they mangle or inject; escape them
- **Syntax errors** - Misspellings break diagrams; validate syntax in Mermaid Live
- **Overcomplexity** - Split complex diagrams into multiple focused views
- **Missing relationships** - Document all important connections between entities

## When to Create Diagrams

**Always diagram when:**
- Starting new projects or features
- Documenting complex systems
- Explaining architecture decisions
- Designing database schemas
- Planning refactoring efforts
- Onboarding new team members

**Use diagrams to:**
- Align stakeholders on technical decisions
- Document domain models collaboratively
- Visualize data flows and system interactions
- Plan before coding
- Create living documentation that evolves with code
