// The document open in this browser tab, shared by the workspace, the boundary editor and the report so
// moving between them never asks for an id again. Kept per tab (sessionStorage): a sample's playground copy
// belongs to the tab that opened it.
const CURRENT_KEY = 'roam.current';
const PLAYGROUND_KEY = 'roam.playground';
const LAST_KEY = 'roam:lastDocumentId';

export function setCurrentDocument(id: string | null) {
  try {
    if (id) sessionStorage.setItem(CURRENT_KEY, id);
    else sessionStorage.removeItem(CURRENT_KEY);
  } catch {
    // storage unavailable: pages fall back to their ?doc= link
  }
}

/** ?doc= first, then this tab's open document, then (for an own document) the last one opened in this browser. */
export function currentDocument(param: string | null): string | null {
  if (param) return param;
  try {
    return sessionStorage.getItem(CURRENT_KEY) ?? localStorage.getItem(LAST_KEY);
  } catch {
    return null;
  }
}

/** Is this one of this tab's playground copies of a sample (never remembered across sessions)? */
export function isPlayground(id: string): boolean {
  try {
    return id in JSON.parse(sessionStorage.getItem(PLAYGROUND_KEY) ?? '{}');
  } catch {
    return false;
  }
}
