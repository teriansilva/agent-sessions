import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import type { Template, TemplatesResponse } from "../../types/api";
import type { DirectPrompt, Variable } from "./playbookDraft";
import { PlaybookPrompts } from "./PlaybookPrompts";

vi.mock("../../lib/api", () => ({
  api: {
    templates: vi.fn(),
    importStepPrompt: vi.fn(),
    compareStepPrompt: vi.fn(),
  },
}));
const source: Template = {
  id: "source",
  name: "Source",
  description: "",
  tags: [],
  body: "Read carefully.",
  fields: [],
  images: [],
  created_at: 1,
  updated_at: 2,
  used_count: 0,
  last_used_at: null,
};
const prompt: DirectPrompt = {
  id: "retained",
  title: "Source",
  body: "Read carefully.",
};
const variables: Variable[] = [{ name: "repository", kind: "text" }];
beforeEach(() => {
  vi.resetAllMocks();
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  vi.mocked(api.templates).mockResolvedValue({
    templates: [source],
    limits: {},
  } as TemplatesResponse);
});
afterEach(cleanup);
function view(
  prompts: DirectPrompt[],
  onChange: (value: DirectPrompt[]) => void,
  inputs = variables,
) {
  return (
    <MemoryRouter>
      <PlaybookPrompts
        prompts={prompts}
        variables={inputs}
        limit={16}
        field="steps[0].prompts"
        onChange={onChange}
      />
    </MemoryRouter>
  );
}
async function startImport() {
  fireEvent.click(
    screen.getByRole("button", { name: "Import template", exact: true }),
  );
  await screen.findByRole("option", { name: "Source", exact: true });
  fireEvent.change(screen.getByLabelText("Template", { exact: true }), {
    target: { value: "source" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Copy into draft", exact: true }),
  );
}
test("a late import refuses to overwrite newer instructions or input definitions", async () => {
  for (const change of ["prompts", "inputs"] as const) {
    let resolve!: (value: { prompt: DirectPrompt }) => void;
    vi.mocked(api.importStepPrompt).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const onChange = vi.fn();
    const mounted = render(view([], onChange));
    await startImport();
    mounted.rerender(
      view(
        change === "prompts" ? [prompt] : [],
        onChange,
        change === "inputs" ? [{ name: "new_input" }] : variables,
      ),
    );
    await act(async () => resolve({ prompt }));
    expect(await screen.findByRole("alert")).toHaveTextContent("draft changed");
    expect(onChange).not.toHaveBeenCalled();
    mounted.unmount();
  }
});
test("unmount discards an in-flight import without calling the former step", async () => {
  let resolve!: (value: { prompt: DirectPrompt }) => void;
  vi.mocked(api.importStepPrompt).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const onChange = vi.fn();
  const mounted = render(view([], onChange));
  await startImport();
  mounted.unmount();
  await act(async () => resolve({ prompt }));
  expect(onChange).not.toHaveBeenCalled();
});
test("a template request does not duplicate under rapid confirmation", async () => {
  let resolve!: (value: { prompt: DirectPrompt }) => void;
  vi.mocked(api.importStepPrompt).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const onChange = vi.fn();
  render(view([], onChange));
  await startImport();
  fireEvent.click(
    screen.getByRole("button", { name: "Copy into draft", exact: true }),
  );
  expect(api.importStepPrompt).toHaveBeenCalledTimes(1);
  await act(async () => resolve({ prompt }));
  await waitFor(() =>
    expect(onChange).toHaveBeenCalledExactlyOnceWith([prompt]),
  );
});
