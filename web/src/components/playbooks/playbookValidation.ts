import { createContext, useContext } from "react";
export const PlaybookValidation = createContext({ field: "", message: "" });
export function useFieldError(field?: string) {
  const issue = useContext(PlaybookValidation);
  return field && (issue.field === field || issue.field.endsWith(`: ${field}`))
    ? issue.message
    : "";
}
