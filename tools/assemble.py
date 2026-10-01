"""Concatenate tools/frontend_parts/*.js into frontend/admin/index.js (the only file that ships)."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parts = ["p1_core.js", "p2_app.js"]
out = "\n".join((root / "tools" / "frontend_parts" / p).read_text(encoding="utf-8") for p in parts)
(root / "frontend" / "admin" / "index.js").write_text(out, encoding="utf-8")
print(f"index.js: {len(out.splitlines())} lines")
