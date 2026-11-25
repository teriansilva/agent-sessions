# Bug: Session context menu (⋯) is inaccessible on mobile and tablet viewports

## Summary
The "⋯" (more actions) menu in the session sidebar is difficult to find or entirely inaccessible on many mobile and tablet devices. This stems from a combination of a CSS breakpoint mismatch and an unreliable visibility rule that hides the menu trigger on devices reporting hover support.

## Investigation
1. **Breakpoint Mismatch:** The app shell defines mobile viewports as $\le$ 800px (where the sidebar becomes an off-canvas drawer). However, `RowMenu.module.css` used a 640px breakpoint for its bottom-sheet transition. On devices between 640px and 800px (like an iPad in portrait mode), the sidebar was a mobile drawer, but the context menu opened as a desktop-style floating popover, creating a fragmented UX.
2. **Invisibility on Touch Devices:** `SessionList.module.css` hid the action buttons by default (`opacity: 0`) and relied on `@media (hover: none)` to show them. Many modern mobile browsers (especially on iPad or high-end Androids) may report hover support, leaving the "⋯" button permanently invisible.
3. **Small Hitarea:** The trigger button was 28x28px, which is below the recommended size for touch targets.

## Fixes Applied
- **Synchronized Breakpoints:** Updated `RowMenu.module.css` and `SessionList.module.css` to use the same 800px breakpoint as the rest of the app shell.
- **Forced Visibility:** Updated `SessionList.module.css` to force `opacity: 1` for action clusters on all viewports $\le$ 800px.
- **Improved Hitarea:** Increased the trigger button size to 32px on mobile viewports.
- **Regression Test:** Added `web/e2e/rowmenu-mobile.spec.ts` with 390px, 700px, 768px, and 800px coverage.

## Verification
- Verified with Playwright `mobile` project and custom viewport tests.
- Confirmed bottom-sheet behavior at 768px.
