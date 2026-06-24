# AI Assistant Capabilities - Use Case Example

**Custom Diagram Creation with AI-Generated Icons and Document Integration**

*Date: May 2026*

---

## Scenario

A solutions architect needs to create a **network architecture diagram** for a project proposal document. Standard draw.io shapes don't capture the look they want - they need custom icons for a cloud gateway, a database cluster, and a security shield. The final diagram should be embedded in a Word document with corporate styling.

---

## What the Assistant Did

### 1. Icon generation

The assistant generated three custom icons using an image model:

- **Cloud gateway** - a flat blue cloud with a directional arrow, transparent background
- **Database cluster** - an orange cylinder stack, minimal style
- **Security shield** - a green shield with a lock symbol

Each icon was generated at 128x128 px, optimized for use as diagram elements.

> *All three image generations were executed in parallel.*

---

### 2. Diagram creation with embedded icons

The assistant created a draw.io diagram using the generated icons as embedded images:

- Clients on the left, connected through the cloud gateway
- Firewall layer with the security shield icon
- Database cluster on the right with replication arrows
- Labels, connection lines, and grouping boxes for logical zones

The icons were embedded directly into the draw.io XML as data URIs - no external file references needed. The diagram renders identically on any machine.

---

### 3. Image editing and refinement

The architect wanted to adjust one of the icons. The assistant:

- **Edited the cloud icon** by adding a small padlock overlay using a text prompt
- **Cut out a logo** from an existing screenshot the user uploaded, extracting just the company badge from the corner of a slide

Both operations produced clean PNG files that were re-embedded into the diagram.

---

### 4. Document integration

The assistant composed the final proposal document in Word format:

- Applied corporate styles (headings, body text, page layout) from a YAML style definition
- Inserted the diagram as a full-width figure with caption
- Combined it with text content from the architect's markdown draft
- Generated a PDF for distribution

---

## Tools & Integrations Used

| Tool | Purpose |
|---|---|
| `generate_image` (generate) | Create custom icons from text descriptions |
| `generate_image` (edit) | Modify an existing image with a text prompt |
| `generate_image` (cut) | Extract a region from a screenshot using vision |
| `generate_diagram` | Create draw.io diagram with embedded custom icons |
| `compose_document` | Assemble final Word/PDF document with diagram |

---

## Key Capabilities Demonstrated

- **AI-generated visual assets** - custom icons and illustrations created on demand, not limited to built-in shape libraries
- **Crossover workflow** - raster images from image models embedded directly into vector diagrams
- **Vision-guided extraction** - cutting specific elements from existing images without manual coordinate input
- **End-to-end document pipeline** - from icon generation to diagram to styled Word document in a single conversation
- **Multi-format output** - diagrams as standalone draw.io files or embedded in Word, PowerPoint, and PDF documents