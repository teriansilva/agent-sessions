# Browser timing checks

The shared runner can delay animation frames and timers. The failures tracked in
issue #1016 came from tests that treated a minimum delay or an intermediate UI state
as completed work.
Their assertions still exercise the production SPA and the actual requests or socket
frames it sends.

| Check | Synchronization | Regression it must catch |
|---|---|---|
| Sending clears a draft | Observe a resize on the open socket before Send; observe the submitted Enter | Sent content survives as a server draft or returns after reload |
| Terminal ANSI colors | Retry the exact CSS color on the current span; keep the injected sample intact | Incorrect light or dark ANSI palette, including after xterm replaces its row nodes |
| First send waits for boot | Observe a connected-socket resize before emitting boot output; control the quiet interval | Lost fake boot chunks, premature delivery, or no paste/Enter after the final quiet interval |
| Keyboard objective reorder | Wait for the announced target position before dropping | Missing/duplicate PATCH, wrong order, or a refused reorder that fails to restore the server order |
| Overview resize burst | Control browser time; wait for each real layout and ResizeObserver delivery between steps | A refit per step, a refit before the trailing debounce, or a final grid that differs from a fresh fit |
| Terminal text-size burst | Control browser time; wait for each actual font change between taps | Intermediate terminal widths, a return to the original width, terminal/socket remount, or lost taps |

The two burst tests pause Playwright's clock after initial connection and advance it
by 16 ms between real inputs. Rendering may take longer on the host; that does not
advance the test's debounce clock. The overview's separate pointer-resize test keeps
exercising real time and grid convergence.

The first-send fixture refuses to emit output without an open socket and receiver.
Its quiet-window checks also control browser time: every boot chunk must reset the
wait, and delivery must remain held until the final quiet interval expires. This
prevents a passing test that silently dropped the first chunk and never exercised
the reset it claimed to test.

The color fixture uses the terminal bench's existing `wipeOnResizeChange: false`
option. Its synthetic palette sample must survive long enough to measure; the generic
bench's repaint prompt would erase it. The shared bench default and the dedicated
resize/history checks retain their wipe behavior. Color assertions resolve the current
span repeatedly because xterm may replace a row between locating it and reading its CSS.
Every expected light and dark palette value remains exact.

A resize frame may change only the row count. The text-size test therefore counts
**consecutive width changes**, starting at the original width. For example,
`118 → 118 → 170` contains one width change; `118 → 127 → 170` and
`118 → 170 → 118 → 170` both fail. Socket and terminal identity are checked separately.

## Reproduce under bounded browser load

Build the SPA first, then run from `web/`. Choose an unused `E2E_PORT` for the checkout.
The following command runs just the relevant cases with one worker, zero retries and
first-attempt failure traces:

```sh
E2E_PORT=48116 npx playwright test \
  e2e/overview-windows.spec.ts e2e/terminal-text-size.spec.ts \
  e2e/mission-objectives-dnd.spec.ts e2e/compose-draft.spec.ts e2e/compose-first-send.spec.ts \
  e2e/light-readability.spec.ts \
  --grep 'a resize burst settles|a burst of taps|sending the message clears|a reorder the server refuses|keyboard: Space|first compose Send|fresh Codex|agent-style ANSI output|terminal surface \+ ANSI' \
  --workers=1 --retries=0 --repeat-each=10 --trace=retain-on-failure
```

Repeat with `E2E_CPU_THROTTLE=4` added to the environment. This slows only the test
page through Chromium's CDP interface. It does not generate host-wide CPU load or
change the application debounce. Record the base SHA, browser version, worker and
repeat counts, first-attempt outcomes and observed host load. Default browser speed
on a shared runner is not an idle-host baseline.

Keep a failing trace's exact assertion and state sequence. A missing reorder request
can mean the drag never reached its target; it does not by itself demonstrate broken
server rollback. A preserved draft after an unconnected Send is expected behavior.
Do not turn these into larger blanket timeouts or discard their wire assertions.

Negative controls belong only in an isolated test checkout: bypass the refit debounce,
retain a refused optimistic order, or suppress the empty draft flush after delivery.
For first-send readiness, stop resetting the quiet timer after the first chunk.
The corresponding tests must fail at their behavior assertion. Restore product code
and rebuild before collecting positive results or opening a PR.

For colors, a diagnostic stylesheet that forces an incorrect terminal color must fail
the exact CSS assertions in both themes and projects. Keep that override in the isolated
diagnostic copy and remove it before collecting positive results.

## Component delivery-window checks

The Compose unit tests for a Return-chip, Up-key or terminal keystroke arriving
between a template paste and its deferred Enter must hold that interval open.
Awaiting two user-event clicks on a busy host can consume the real 120 ms window;
an assertion expecting only the clear and paste then sees all four frames.

These three checks fill the real picker first, pause the test clock, and dispatch
the send and intervening input through the component. At 119 ms only clear and
paste may have arrived. At 120 ms the exact sequence must be clear, paste, Enter,
then the held input. Usage accounting and the terminal input behavior outside the
window remain asserted. The shared cleanup restores real timers after each test.

Run them from `web/`:

```sh
npx vitest run src/components/terminal/Compose.test.tsx \
  -t "the Return chip rides|a key-bar Up inside|a keystroke typed into the terminal is HELD"
```

As a negative control in a separate diagnostic copy, bypass `deferInput`'s hold;
all three checks must fail before the clock advances. Restore that copy before
collecting positive results. No production timer or input-fence behavior changes.

## Unit-suite resource budget and poll startup

Vitest runs at most four workers by default. Its CPU-count default can otherwise
start 63 workers per invocation on a 64-CPU shared host, independently in each
concurrent job. This cap changes process concurrency, not test selection, deadlines
or retries. Use `npx vitest run --maxWorkers=N` only for an explicit measurement.
The complete suite remains required; a focused pass does not clear a failed run.

The mission settlement-retry check must first observe the pending mission, because
starting its initial request does not mount the pending-state poll. It then verifies
all 40 fast attempts occurred before testing the slow settlement/read-failure path.
A delayed initial response reproduces the old setup race. In a separate diagnostic
copy, removing the retry-debt branch must still fail the final recovery assertion.
Restore the hook before collecting positive results.
