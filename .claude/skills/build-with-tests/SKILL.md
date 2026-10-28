---
name: build-with-tests
description: Build, implement, add, or extend a feature in this project, with unit tests written alongside the code. Use whenever the user asks to build, implement, add, create, or extend a page, component, content collection, script, or other feature. It covers reading CLAUDE.md and the technical brief first, matching 2-3 existing similar features, writing tests as part of normal engineering, and finishing with typecheck, lint, and the test suite.
---

# Build a production feature

Follow this workflow for any feature work. Its goal is code that looks like the rest of the project and ships with passing checks.

## 1. Read before you write
- Read `CLAUDE.md` in full. It sets the stack, non-goals, conventions, and guardrails. A feature that breaks a non-goal (for example, adding server-side code or a heavy JS framework) is wrong even if it works.
- Read the technical brief or task description. If there is none, restate the requirements in a few lines and confirm anything ambiguous before coding.
- Note the acceptance criteria. These decide which tests you write.

## 2. Study 2-3 similar features
- Find existing features closest to the one you're building (Glob/Grep under `src/pages`, `src/components`, `src/layouts`, `src/content`). The `codebase-researcher` subagent can do this if the area is large.
- Read them end to end: structure, naming, props and types, how content is loaded, how tests are laid out.
- Match what you find. If the codebase has no close analogue, say so and follow `CLAUDE.md`. Don't invent a new pattern when an existing one fits.

## 3. Build with tests alongside
- Write unit tests as you go, in the same change as the code. This is ordinary good engineering, not a red-green TDD loop. Test as soon as a piece is shaped enough to test.
- Test behavior and edge cases (empty input, missing fields, invalid content), not implementation details.
- Put logic that can be tested (data shaping, formatting, validation) in plain TypeScript functions, so tests don't need to render pages.
- Keep components small and compose them.

## 4. Verify before reporting done
Run these in order and fix failures at the root cause:
1. `npm run check`: type and content-schema errors (`astro check`)
2. `npm run lint`: ESLint
3. `npm test`: unit tests
4. `npm run build`: confirm the static build still succeeds

Report the real results. If a check fails or you skipped one, say so. Don't claim success without running them.

## 5. Project conventions
From `CLAUDE.md`:
- TypeScript strict mode for scripts and components.
- Mobile-first and responsive; verify at 375px and 1280px.
- Accessibility: semantic HTML, alt text on every image, visible focus states, AA contrast.
- No client-side JS by default; use Astro islands only when needed; use `astro:assets` for images.
- Support light and dark mode via `prefers-color-scheme`.
- Every page gets a unique `<title>`, meta description, and Open Graph tags.
- Never hardcode the domain; read the site URL from `site` in `astro.config.mjs`.
- Never invent facts about Alex. Leave a marked `TODO:` placeholder and flag it.
- Don't commit secrets, don't delete content files without asking, and don't run deploy commands.

TODO (fill in once the codebase has established patterns; don't guess):
- **Naming:** file, component, and function naming
- **Error handling:** how failures and bad content are surfaced
- **Where business logic lives:** e.g. a `src/lib/` or `src/utils/` module, kept out of `.astro` files
- **Test structure:** test runner, file location and naming, and how tests are organized
