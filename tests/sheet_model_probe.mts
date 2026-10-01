// Prints the sheet model the frontend builds for one stored result.json (used by test_sheet_model_real_documents.py).
import fs from 'node:fs';
import { boundaryRefs, groupCandidates, sheetOf, primaryCandidates } from '../frontend/src/lib/boundaryCandidates.ts';
const result = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const refs = boundaryRefs(result);
console.log(JSON.stringify({
  sheets: Object.fromEntries((result.pages ?? []).map((p: any) => [p.page_number, sheetOf(p)]).filter(([, s]: any) => s)),
  groups: groupCandidates(refs).map((g) => ({
    page: g.pageNumber, primary: g.primary, badge: g.badge, note: g.note,
    items: g.refs.map((r) => ({ label: r.label, isRegion: r.isRegion, region: r.regionIndex }))
  })),
  excluded: refs.filter((r) => r.excludedReason).map((r) => ({ page: r.pageNumber, label: r.label })),
  primaryParcels: primaryCandidates(refs).length
}));
