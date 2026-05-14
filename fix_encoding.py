from pathlib import Path
import ast

root = Path(__file__).parent

# After the previous PowerShell fix, the curly right quote inside em-dash
# mojibake sequences was replaced with a straight quote, creating false string
# terminators. Fix by replacing the whole mojibake + trailing straight quote.
REPLACEMENTS = {
    "--": "--",   # -- with curly right quote still present
    "--\"": "--",        # -- where right quote was already straightened
    "'": "'",     # --™ single quote mojibake
    "'": "'",     # --˜ left single quote mojibake
    """: '"',     # --œ left double quote mojibake
    "--: "--",          # catch remaining -- fragments
}

fixed_files = 0
for p in root.rglob("*.py"):
    if "__pycache__" in str(p):
        continue
    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Could not read {p.name}: {e}")
        continue

    new = content
    for bad, good in REPLACEMENTS.items():
        new = new.replace(bad, good)

    if new != content:
        p.write_text(new, encoding="utf-8")
        fixed_files += 1
        print(f"Fixed: {p.name}")

errors = []
for p in root.rglob("*.py"):
    if "__pycache__" in str(p):
        continue
    try:
        ast.parse(p.read_bytes())
    except SyntaxError as e:
        errors.append((p.name, e.lineno, str(e)))

print(f"\nFixed {fixed_files} files.")
print(f"Remaining syntax errors: {len(errors)}")
for name, line, msg in errors:
    print(f"  {name}:{line} -- {msg}")
