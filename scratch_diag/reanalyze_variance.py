import re, json

TARGET_VALUES = ["220.00", "352.40", "550.75", "1077.91", "2637.36", "325.30", "329.80", "336.41"]
PIECE_RE = re.compile(r"Piece\s+(\d+)")

def split_by_piece(text):
    matches = list(PIECE_RE.finditer(text))
    sections = {}
    for i, m in enumerate(matches):
        piece_num = int(m.group(1))
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        sections.setdefault(piece_num, "")
        sections[piece_num] += text[start:end]
    return sections

run_sections = []
for i in range(1, 6):
    text = open(f"scratch_diag/trace_tiles/variance_run{i}.txt", encoding="utf-8").read()
    run_sections.append(split_by_piece(text))

for v in TARGET_VALUES:
    print(f"--- {v} ---")
    for run_idx, sections in enumerate(run_sections, start=1):
        hits = [piece for piece, text in sections.items() if v in text]
        print(f"  run {run_idx}: pieces {hits if hits else 'NOT FOUND'}")
    print()
