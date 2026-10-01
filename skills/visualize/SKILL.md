---
type: skill
name: visualize
description: "Add diagrams and visual explanations to lessons when a picture carries information prose cannot"
---

## When to use

A diagram helps when the idea is **structural** (hierarchies, layers), **spatial** (layouts, geometry), **relational** (flows, graphs, state machines), or **comparative** (side-by-side that tables muddle). If prose already conveys it clearly, skip.

## Workflow

1. **Identify** — teaching agent spots a concept that would be clearer as a picture.
2. **Prune** — strip to the fewest elements that convey the idea. Every box and arrow must earn its place.
3. **Generate** — call `diagram-design` skill or use `eval`/`write` to produce the diagram:
   - **Mermaid** → flows, graphs, state machines (embed as fenced code block or render to SVG).
   - **SVG** → geometric, spatial, anatomical diagrams (write directly or via `eval`).
   - **Architecture** → layer stacks, data-flow, integration (via `diagram-design`).
4. **Save** — write output to the workspace `assets/` or `reference/` directory as PNG or SVG.
5. **Embed** — insert a markdown image link (`![alt](./assets/name.svg)`) or MDX-compatible import in the lesson.

## Anti-patterns

- **Don't decorate.** A diagram must carry unique information prose doesn't.
- **Don't visualize trivia.** Bullet points beat a diagram for simple lists.
- **Don't over-label.** If the legend is longer than the diagram, redraw.

## Diagram types

| Need | Type | Tool |
|------|------|------|
| Flow / graph / state | Mermaid | `eval` code block → SVG |
| Geometric / spatial | SVG | `write` or `eval` |
| Architecture / layers | HTML/SVG | `diagram-design` skill |
| Comparison / matrix | Visual table | `diagram-design` or SVG |
