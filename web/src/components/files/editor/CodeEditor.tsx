import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { bracketMatching, indentOnInput } from "@codemirror/language";
import { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState, Prec, Transaction } from "@codemirror/state";
import {
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { languageFor } from "./languages";
import { battlelabHighlight, battlelabTheme } from "./theme";

/** The file viewer's editor (#950) — the one component that talks to CodeMirror.
 *
 *  Everything outside `components/files/editor/` goes through this component; an import-scan test
 *  (`importSeam.test.ts`) fails the build otherwise, so replacing the editor later is one directory.
 *
 *  Choices that are contracts rather than taste:
 *
 *  * **Tab is not bound.** The viewer is a modal with a focus trap; an editor that swallowed Tab would
 *    trap keyboard users inside the document. Indentation is Mod-] / Mod-[ (CodeMirror's defaults).
 *  * **Read-only never raises a phone keyboard.** Read mode keeps the content focusable (so search
 *    and selection work) but sets `inputmode="none"`; edit mode drops it.
 *  * **Swapping the document programmatically is not an edit.** A reload, or showing the on-disk
 *    version during a conflict, is kept out of undo history and does not flip the dirty flag.
 *  * **Dirty means "differs from the last saved text"**, not "was typed in": undoing back to the
 *    saved text is clean again.
 */
export interface CodeEditorHandle {
  getText(): string;
  /** Replace the document AND make it the clean baseline — after a reload or a save. */
  reset(text: string): void;
  /** Show different text without touching the baseline or the dirty flag (e.g. ON DISK). */
  show(text: string): void;
  /** Make the current document (or `text`) the clean baseline. */
  markClean(text?: string): void;
  /** Whether the document differs from the baseline RIGHT NOW. Synchronous on purpose: a close
   *  or save decided from React state can run before the render that carries the last keystroke,
   *  and on Android CodeMirror applies typed input a moment after the DOM changes. Either race
   *  reads "clean" and closes the viewer over unsaved text — measured in the mobile e2e. */
  isDirty(): boolean;
  focus(): void;
}

const normalize = (text: string) => text.replace(/\r\n?/g, "\n");

function editableExtensions(editable: boolean) {
  return [
    EditorView.editable.of(true),
    EditorState.readOnly.of(!editable),
    EditorView.contentAttributes.of(
      editable
        ? { "aria-readonly": "false" }
        : { "aria-readonly": "true", inputmode: "none" },
    ),
  ];
}

export default function CodeEditor({
  ref,
  text,
  path,
  editable,
  label,
  onDirtyChange,
  onSave,
  onCursor,
}: {
  ref?: Ref<CodeEditorHandle>;
  /** The initial document. Later changes come in through the handle, never by re-render. */
  text: string;
  path: string;
  editable: boolean;
  label: string;
  onDirtyChange?: (dirty: boolean) => void;
  onSave?: () => void;
  onCursor?: (line: number, col: number) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const baseline = useRef(normalize(text));
  const dirty = useRef(false);
  const quiet = useRef(false);
  const callbacks = useRef({ onDirtyChange, onSave, onCursor });
  const [compartments] = useState(() => ({
    editable: new Compartment(),
    language: new Compartment(),
  }));

  useEffect(() => {
    callbacks.current = { onDirtyChange, onSave, onCursor };
  });

  // Mount once. `text` and `editable` flow in through the handle and the effect below, so a parent
  // re-render never rebuilds the view (which would drop the cursor, the scroll and undo history).
  useEffect(() => {
    const parent = host.current;
    if (!parent) return;
    // On a phone, long lines wrap instead of panning the document sideways under a sticky gutter —
    // horizontal scrolling while typing on a 390px screen hides the text being edited.
    const coarse = window.matchMedia?.("(pointer: coarse)").matches ?? false;
    const setDirty = (next: boolean) => {
      if (next === dirty.current) return;
      dirty.current = next;
      callbacks.current.onDirtyChange?.(next);
    };
    const view = new EditorView({
      parent,
      state: EditorState.create({
        doc: text,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          highlightSpecialChars(),
          history(),
          drawSelection(),
          indentOnInput(),
          battlelabHighlight,
          bracketMatching(),
          highlightActiveLine(),
          highlightSelectionMatches(),
          search({ top: true }),
          Prec.high(
            keymap.of([
              {
                key: "Mod-s",
                preventDefault: true,
                run: () => {
                  callbacks.current.onSave?.();
                  return true;
                },
              },
            ]),
          ),
          keymap.of([...searchKeymap, ...historyKeymap, ...defaultKeymap]),
          compartments.editable.of(editableExtensions(editable)),
          compartments.language.of([]),
          battlelabTheme,
          coarse ? EditorView.lineWrapping : [],
          EditorView.contentAttributes.of({ "aria-label": label }),
          EditorView.updateListener.of((u) => {
            if (u.selectionSet || u.docChanged) {
              const head = u.state.selection.main.head;
              const line = u.state.doc.lineAt(head);
              callbacks.current.onCursor?.(line.number, head - line.from + 1);
            }
            if (!u.docChanged || quiet.current) return;
            const doc = u.state.doc;
            setDirty(
              doc.length !== baseline.current.length || doc.toString() !== baseline.current,
            );
          }),
        ],
      }),
    });
    viewRef.current = view;
    let live = true;
    const language = languageFor(path);
    language
      ?.load()
      .then((ext) => {
        if (live) view.dispatch({ effects: compartments.language.reconfigure(ext) });
      })
      .catch(() => {
        /* a language chunk that fails to load leaves plain text, which is still correct */
      });
    return () => {
      live = false;
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount once; see the comment above
  }, []);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: compartments.editable.reconfigure(editableExtensions(editable)),
    });
  }, [editable, compartments]);

  useImperativeHandle(
    ref,
    () => {
      const replace = (next: string) => {
        const view = viewRef.current;
        if (!view) return;
        quiet.current = true;
        try {
          view.dispatch({
            changes: { from: 0, to: view.state.doc.length, insert: next },
            annotations: Transaction.addToHistory.of(false),
          });
        } finally {
          quiet.current = false;
        }
      };
      const recompute = () => {
        const view = viewRef.current;
        const now = view ? view.state.doc.toString() !== baseline.current : false;
        if (now !== dirty.current) {
          dirty.current = now;
          callbacks.current.onDirtyChange?.(now);
        }
      };
      // Apply any DOM input CodeMirror has observed but not yet dispatched. `observer` is not public
      // API, so this is guarded: if it ever moves, the call is a no-op and reads still reflect every
      // dispatched change.
      const flush = (view: EditorView) =>
        (view as unknown as { observer?: { flush?: () => void } }).observer?.flush?.();
      return {
        getText: () => {
          const view = viewRef.current;
          if (!view) return baseline.current;
          // A draft read for OVERWRITE or ON DISK must include the keystroke still in the DOM.
          flush(view);
          return view.state.doc.toString();
        },
        reset: (next) => {
          baseline.current = normalize(next);
          replace(next);
          recompute();
        },
        show: (next) => replace(next),
        markClean: (next) => {
          baseline.current = normalize(next ?? viewRef.current?.state.doc.toString() ?? "");
          recompute();
        },
        isDirty: () => {
          const view = viewRef.current;
          if (!view) return dirty.current;
          flush(view);
          const doc = view.state.doc;
          return doc.length !== baseline.current.length || doc.toString() !== baseline.current;
        },
        focus: () => viewRef.current?.focus(),
      };
    },
    [],
  );

  return <div ref={host} className="battlelab-code-editor" data-code-editor="" />;
}
