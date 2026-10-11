# Region trial (experiment, not part of ROAM)

Throwaway scripts that test whether ROAM could work outside the US. Nothing here is imported by the
app, and the main pipeline is untouched.

- `trial.py`: region detection, worldwide geocoding, whole-circle and quadrant bearing parsing
- `trial2.py`: the full flow with timings: region gate, LINZ parcel lookup (New Zealand), Ordnance
  Survey grid reference (UK), street geocoding, and a regression check on the six US samples
- `bearings.py`: reads whole-circle bearings from New Zealand survey plans and checks closure
- `results_v1.json`, `results_v2.json`: the measured results

Run from the repository root with the local backend's environment, for example
`roam-env/Scripts/python -X utf8 experiments/region_trial/trial2.py`. It needs `LINZ_KEY` in `.env`
(a free key from data.linz.govt.nz) and the test documents in `data/documents/` (not in git).

Findings: region detection was right on 7 of 7 documents (6 of 6 US samples stayed US); LINZ and the
UK grid reference gave verified or near placements; street geocoding alone is only approximate.
