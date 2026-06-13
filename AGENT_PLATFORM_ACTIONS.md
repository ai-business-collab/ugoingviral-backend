# In-Platform Agent — Platform Action Verification

Verified 2026-06-13 against the live server (`/api/agent/chat`, `gpt` agent
orchestrator with Claude fallback). The agent is an **LLM assistant that guides
the UI** — it returns conversational text plus optional UI *actions*
(`[[NAV:<page>]]`, `[[BTN:<label>:<action>]]`, `[[ESCALATE]]`). It does **not**
call posting/scheduling/analytics endpoints itself; the frontend dispatches the
returned actions. All requests below returned HTTP 200.

## Commands tested

| Command (Danish)                              | Response                                                                 | Actions returned            |
|-----------------------------------------------|--------------------------------------------------------------------------|-----------------------------|
| "Lav et TikTok opslag om vores nye produkt"   | Asks for product details, then offers to draft the caption              | `navigate→generator` (varies) |
| "Planlæg 3 opslag til næste uge"              | Asks which topics/messages to include for each post                     | none / 1 nav (varies)       |
| "Vis mig mine statistikker"                   | Reports **real** state — "no content in 30 days, no platforms connected" (does not fabricate numbers) | `navigate→generator`        |
| "Lav et opslag om [topic]" (generic)          | Returns a generated caption draft (~200 chars)                          | varies                      |

## What works

- **Responds correctly and in Danish** to all platform commands (200 OK).
- **Generates post content** on request ("lav et opslag om …" returns a usable caption draft).
- **Surfaces navigation actions** (`[[NAV:generator]]`) to route the user to the right tool.
- **Honors the real-data principle**: "Vis mig mine statistikker" reports the
  true empty state instead of inventing statistics.
- Content generation (`/api/content/generate`) returns content **without**
  publishing to any social platform (verified: scheduled-post count unchanged).
- Auto Pilot enable/disable, scheduled-post create + list all work via API
  (see `qa_agent.py` suite 17).

## What needs improvement

1. **No direct execution.** The agent does not call `/api/content/generate`,
   `/api/posts/schedule`, or analytics endpoints itself — it asks follow-up
   questions and navigates. A "Lav et TikTok opslag om X" with enough detail
   should be able to return a finished caption **and** a one-click
   `[[BTN:Planlæg opslag:schedule_post]]` action.
2. **Inconsistent navigation targets.** "Vis mig mine statistikker" navigates to
   `generator` rather than an analytics/stats page.
3. **Action emission is non-deterministic.** The same command sometimes returns
   a NAV action and sometimes none (LLM variance). "Planlæg 3 opslag" in
   particular often returns no action linking to the scheduler/calendar.
4. **No multi-post scheduling shortcut.** "Planlæg 3 opslag til næste uge" is not
   wired to bulk-create scheduled posts; it stays conversational.

### Suggested fixes
- Add explicit `[[BTN:…:schedule_post]]` / `[[NAV:analytics]]` / `[[NAV:calendar]]`
  examples to the agent system prompt so the LLM emits the correct action per
  intent reliably.
- Consider a deterministic intent router for post-creation/scheduling/stats that
  attaches the correct action regardless of LLM phrasing.
