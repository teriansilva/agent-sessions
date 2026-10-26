/** Settings → Agents → one agent: which grade each of its models is (#1693, #1613 D18). The map
 *  ships empty — no catalog carries tiers, and a guessed tier would mislabel a model. A specialist's
 *  grade is derived from it at read, so a change here re-grades every specialist on that model. */
import { useEffect, useState } from "react";
import { useConfigReload } from "../../app/config";
import { offeredModels } from "../../app/engineRoster";
import { api, ApiError } from "../../lib/api";
import type { Grade } from "../../types/specialists";
import {
  confirmGrades,
  GRADES,
  GRADE_LABEL,
  type ModelGrades,
  useModelGrades,
} from "./grades";
import styles from "./specialists.module.css";

export function ModelGrades({ engine }: { engine: { id: string; runtime: string } }) {
  const reload = useConfigReload();
  const stored = useModelGrades()?.[engine.id] ?? {};
  const [apiModels, setApiModels] = useState<{ id: string; label: string }[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (engine.runtime !== "api") return;
    void api
      .structuredModels(engine.id)
      .then((l) => setApiModels(l.models.map((m) => ({ id: m.id, label: m.label }))))
      .catch(() => setApiModels([]));
  }, [engine.id, engine.runtime]);
  const models =
    engine.runtime === "api"
      ? (apiModels ?? [])
      : offeredModels(engine.id).map((m) => ({ id: m.id, label: m.id }));

  const save = async (model: string, grade: Grade | "") => {
    setBusy(true);
    setError("");
    try {
      // A patch of ONE row (`null` clears it): the server merges under its lock, so two tabs
      // grading different models never erase each other's work.
      const out = await api.setPrefs({
        model_grades: { [engine.id]: { [model]: grade || null } },
      });
      // The server answers with the map it committed and that commit's revision: the newest map
      // seen wins, whatever order saves commit or answers arrive in.
      const merged = out.model_grades as ModelGrades | undefined;
      const rev = out.model_grades_rev;
      if (merged && typeof rev === "number") confirmGrades(merged, rev);
      if (!(await reload())) {
        setError("Saved, but this page couldn’t reload your settings. Reload to be sure.");
      }
    } catch (e) {
      setError(e instanceof ApiError && e.message ? e.message : "Couldn’t save the grade.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.gradesTable} data-testid="model-grades">
      <p className={styles.hint}>
        A specialist’s grade comes from its model. Intern = fast and cheap, Specialist =
        strong, Senior = frontier. A model with no grade can only fill a role that asks for
        any grade.
      </p>
      {models.length === 0 ? (
        <p className={styles.hint}>This agent reports no models to grade.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Grade</th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <tr key={m.id}>
                <th scope="row">
                  <code>{m.id}</code>
                  {m.label !== m.id && <span> {m.label}</span>}
                </th>
                <td>
                  <select
                    aria-label={`Grade for ${m.id}`}
                    value={stored[m.id] ?? ""}
                    disabled={busy}
                    onChange={(e) => void save(m.id, e.target.value as Grade | "")}
                  >
                    <option value="">No grade</option>
                    {GRADES.map((g) => (
                      <option key={g} value={g}>
                        {GRADE_LABEL[g]}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && <p role="alert" className={styles.refused}>{error}</p>}
    </div>
  );
}
