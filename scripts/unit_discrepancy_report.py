#!/usr/bin/env python3
"""
Report every unit and material-link discrepancy in the live database.

Run from the backend directory:

    .venv/bin/python scripts/unit_discrepancy_report.py            # text
    .venv/bin/python scripts/unit_discrepancy_report.py --html     # shareable

Reads DATABASE_URL from backend/.env. Read-only — issues SELECTs and nothing
else.

Why this exists: BOQ lines, purchase-order lines and materials are linked by
case-insensitive *name*, not a foreign key, and each carries its own free-text
unit. So two things can silently go wrong, and neither raises an error:

  1. A BOQ material name does not match any material, so its consumption is
     never recorded at all.
  2. The BOQ unit and the material's stock unit measure different things, so
     the quantity is deducted in the wrong unit.

Section A is blocking (work-order completion now refuses these). Section B is
already handled by conversion and needs no action. Section C and beyond are
data-hygiene findings.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, text  # noqa: E402

from app.unit_conversion import convert_qty, dimension_of  # noqa: E402


def _database_url() -> str:
    env = BACKEND / ".env"
    if not env.exists():
        sys.exit("backend/.env not found — cannot read DATABASE_URL")
    for line in env.read_text().splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("DATABASE_URL not set in backend/.env")


QUERIES = {
    # BOQ line -> material, where the two exist and units differ
    "boq_pairs": """
        SELECT p.name AS product, b.name AS material,
               b.units AS boq_unit, m.unit AS stock_unit,
               COUNT(*) AS boq_lines
        FROM bill_of_quantities b
        JOIN product_bill_of_quantity_relations r ON r.bill_of_quantity_id = b.id
        JOIN products p ON p.id = r.product_id
        JOIN materials m ON LOWER(TRIM(m.name)) = LOWER(TRIM(b.name))
        WHERE LOWER(TRIM(COALESCE(b.units,''))) <> LOWER(TRIM(COALESCE(m.unit,'')))
        GROUP BY 1,2,3,4
        ORDER BY 1,2
    """,
    # BOQ lines whose material name matches nothing in inventory
    "orphan_boq": """
        SELECT p.name AS product, b.name AS material, b.units AS boq_unit,
               COUNT(*) AS boq_lines
        FROM bill_of_quantities b
        JOIN product_bill_of_quantity_relations r ON r.bill_of_quantity_id = b.id
        JOIN products p ON p.id = r.product_id
        WHERE NOT EXISTS (
            SELECT 1 FROM materials m
            WHERE LOWER(TRIM(m.name)) = LOWER(TRIM(b.name))
        )
        GROUP BY 1,2,3
        ORDER BY 1,2
    """,
    # PO line -> material, units differ
    "po_pairs": """
        SELECT po.purchase_number, l.material_name AS material,
               l.unit AS po_unit, m.unit AS stock_unit, po.status
        FROM purchase_order_lines l
        JOIN purchase_orders po ON po.id = l.purchase_order_id
        JOIN materials m ON LOWER(TRIM(m.name)) = LOWER(TRIM(l.material_name))
        WHERE LOWER(TRIM(COALESCE(l.unit,''))) <> LOWER(TRIM(COALESCE(m.unit,'')))
        ORDER BY po.purchase_number
    """,
    # Which work orders are affected, and are they still open
    "affected_wos": """
        SELECT w.work_order_number, w.party_name, w.status,
               p.name AS product, b.name AS material,
               b.units AS boq_unit, m.unit AS stock_unit
        FROM work_orders w
        JOIN work_order_products wp ON wp.work_order_id = w.id
        JOIN products p ON p.id = wp.product_id
        JOIN product_bill_of_quantity_relations r ON r.product_id = p.id
        JOIN bill_of_quantities b ON b.id = r.bill_of_quantity_id
        JOIN materials m ON LOWER(TRIM(m.name)) = LOWER(TRIM(b.name))
        WHERE LOWER(TRIM(COALESCE(b.units,''))) <> LOWER(TRIM(COALESCE(m.unit,'')))
        ORDER BY w.status, w.work_order_number
    """,
    # Every distinct unit spelling in use, per column
    "unit_usage": """
        SELECT 'materials.unit' AS source, COALESCE(unit,'(null)') AS unit, COUNT(*) AS rows
        FROM materials GROUP BY 2
        UNION ALL
        SELECT 'bill_of_quantities.units', COALESCE(units,'(null)'), COUNT(*)
        FROM bill_of_quantities GROUP BY 2
        UNION ALL
        SELECT 'purchase_order_lines.unit', COALESCE(unit,'(null)'), COUNT(*)
        FROM purchase_order_lines GROUP BY 2
        ORDER BY 1, 3 DESC
    """,
    # Names differing only by case or surrounding whitespace
    "near_dupe_materials": """
        SELECT LOWER(TRIM(name)) AS normalized,
               STRING_AGG(DISTINCT '"' || name || '"', ' | ') AS spellings,
               COUNT(*) AS rows
        FROM materials
        GROUP BY 1 HAVING COUNT(*) > 1
        ORDER BY 3 DESC
    """,
    "negative_stock": """
        SELECT name, length_weight_nos, unit FROM materials
        WHERE length_weight_nos < 0 ORDER BY length_weight_nos
    """,
    "blank_units": """
        SELECT 'materials' AS tbl, name AS item, COUNT(*) AS rows
        FROM materials WHERE COALESCE(TRIM(unit),'') = '' GROUP BY 1,2
        UNION ALL
        SELECT 'bill_of_quantities', name, COUNT(*)
        FROM bill_of_quantities WHERE COALESCE(TRIM(units),'') = '' GROUP BY 1,2
        ORDER BY 1,2
    """,
    "whitespace_units": """
        SELECT 'materials.unit' AS source, unit FROM materials
        WHERE unit IS NOT NULL AND unit <> TRIM(unit)
        UNION ALL
        SELECT 'bill_of_quantities.units', units FROM bill_of_quantities
        WHERE units IS NOT NULL AND units <> TRIM(units)
        UNION ALL
        SELECT 'purchase_order_lines.unit', unit FROM purchase_order_lines
        WHERE unit IS NOT NULL AND unit <> TRIM(unit)
    """,
}


def classify(a: str, b: str) -> str:
    """BLOCKING if the pair cannot convert, OK if it can."""
    return "OK" if convert_qty(1.0, a or "", b or "") is not None else "BLOCKING"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", action="store_true", help="emit an HTML report")
    ap.add_argument("--out", help="write to this path instead of stdout")
    args = ap.parse_args()

    url = re.sub(r"^postgresql\+\w+://", "postgresql+psycopg://", _database_url())
    engine = create_engine(url)

    data: dict[str, list[dict]] = {}
    with engine.connect() as conn:
        for key, sql in QUERIES.items():
            data[key] = [dict(r) for r in conn.execute(text(sql)).mappings().all()]

    # Split BOQ pairs by whether conversion can resolve them
    blocking, benign = [], []
    for row in data["boq_pairs"]:
        row["verdict"] = classify(row["boq_unit"], row["stock_unit"])
        (blocking if row["verdict"] == "BLOCKING" else benign).append(row)

    po_blocking = [
        r for r in data["po_pairs"]
        if classify(r["po_unit"], r["stock_unit"]) == "BLOCKING"
    ]

    open_wos = [
        r for r in data["affected_wos"]
        if r["status"] != "completed"
        and classify(r["boq_unit"], r["stock_unit"]) == "BLOCKING"
    ]

    render = _render_html if args.html else _render_text
    out = render(data, blocking, benign, po_blocking, open_wos)

    if args.out:
        Path(args.out).write_text(out)
        print(f"written to {args.out}")
    else:
        print(out)


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "    (none)\n"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    head = "  " + "  ".join(c.ljust(widths[c]) for c in cols)
    rule = "  " + "  ".join("-" * widths[c] for c in cols)
    body = "\n".join(
        "  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols)
        for r in rows
    )
    return f"{head}\n{rule}\n{body}\n"


def _render_text(data, blocking, benign, po_blocking, open_wos) -> str:
    L = []
    w = L.append
    w("=" * 78)
    w("E-SAFE ERP — UNIT & MATERIAL-LINK DISCREPANCY REPORT")
    w("=" * 78)
    w("")
    w("SUMMARY")
    w(f"  A. BOQ lines that BLOCK work-order completion ... {len(blocking)}")
    w(f"     of which sit on a work order still open ..... {len(open_wos)}")
    w(f"  B. BOQ unit differences handled by conversion ... {len(benign)}")
    w(f"  C. BOQ materials missing from inventory ......... {len(data['orphan_boq'])}")
    w(f"  D. PO lines with an unconvertible unit ......... {len(po_blocking)}")
    w(f"  E. Material names differing only by case/space .. {len(data['near_dupe_materials'])}")
    w(f"  F. Materials with negative stock ................ {len(data['negative_stock'])}")
    w(f"  G. Rows with a blank unit ....................... {len(data['blank_units'])}")
    w(f"  H. Units with stray whitespace .................. {len(data['whitespace_units'])}")
    w("")
    w("-" * 78)
    w("A. BLOCKING — work-order completion refuses these")
    w("-" * 78)
    w("   The BOQ unit and the stock unit measure different things, so the")
    w("   quantity cannot be converted. Fix the BOQ unit or the material's unit.")
    w("")
    w(_table(blocking, ["product", "material", "boq_unit", "stock_unit", "boq_lines"]))
    w("A1. Of those, on a work order that is NOT yet completed:")
    w(_table(open_wos, ["work_order_number", "party_name", "product", "material",
                        "boq_unit", "stock_unit"]))
    w("-" * 78)
    w("B. NO ACTION — different unit, but conversion handles it")
    w("-" * 78)
    w(_table(benign, ["product", "material", "boq_unit", "stock_unit", "boq_lines"]))
    w("-" * 78)
    w("C. BOQ materials with no matching inventory record")
    w("-" * 78)
    w("   Linked by name, so a typo or rename breaks the link silently.")
    w("   Work-order completion refuses these too.")
    w("")
    w(_table(data["orphan_boq"], ["product", "material", "boq_unit", "boq_lines"]))
    w("-" * 78)
    w("D. Purchase-order lines whose unit cannot convert to stock")
    w("-" * 78)
    w("   Receipt still records the quantity (the goods arrived) but without")
    w("   conversion, and now warns on screen. These stock figures need review.")
    w("")
    w(_table(po_blocking, ["purchase_number", "material", "po_unit", "stock_unit"]))
    w("-" * 78)
    w("E. Material names differing only by case or whitespace")
    w("-" * 78)
    w(_table(data["near_dupe_materials"], ["normalized", "spellings", "rows"]))
    w("-" * 78)
    w("F. Negative stock")
    w("-" * 78)
    w(_table(data["negative_stock"], ["name", "length_weight_nos", "unit"]))
    w("-" * 78)
    w("G. Blank units")
    w("-" * 78)
    w(_table(data["blank_units"], ["tbl", "item", "rows"]))
    w("-" * 78)
    w("H. Units with stray whitespace")
    w("-" * 78)
    w(_table(data["whitespace_units"], ["source", "unit"]))
    w("-" * 78)
    w("I. Every unit spelling currently in use")
    w("-" * 78)
    w(_table(data["unit_usage"], ["source", "unit", "rows"]))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------

def _html_table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return '<p class="none">Nothing found — nothing to fix here.</p>'
    th = "".join(f"<th>{html.escape(c.replace('_',' '))}</th>" for c in cols)
    trs = []
    for r in rows:
        tds = "".join(f"<td>{html.escape(str(r.get(c,'')))}</td>" for c in cols)
        trs.append(f"<tr>{tds}</tr>")
    return (
        f'<div class="scroll"><table><thead><tr>{th}</tr></thead>'
        f'<tbody>{"".join(trs)}</tbody></table></div>'
    )


def _render_html(data, blocking, benign, po_blocking, open_wos) -> str:
    counts = [
        ("Block work-order completion", len(blocking), "bad"),
        ("…on a still-open work order", len(open_wos), "bad"),
        ("Handled by conversion", len(benign), "ok"),
        ("Materials missing from inventory", len(data["orphan_boq"]), "bad"),
        ("PO lines needing review", len(po_blocking), "warn"),
        ("Near-duplicate material names", len(data["near_dupe_materials"]), "warn"),
        ("Negative stock", len(data["negative_stock"]), "bad"),
        ("Blank units", len(data["blank_units"]), "warn"),
    ]
    cards = "".join(
        f'<div><span class="n n-{cls}">{n}</span><span class="l">{html.escape(label)}</span></div>'
        for label, n, cls in counts
    )

    sections = [
        ("A. Blocking — work-order completion refuses these",
         "The BOQ unit and the stock unit measure different things, so the quantity "
         "cannot be converted. Fix the BOQ unit or the material's unit so both measure "
         "the same thing.",
         _html_table(blocking, ["product", "material", "boq_unit", "stock_unit", "boq_lines"])),
        ("A1. Of those, on a work order not yet completed",
         "These are the ones that will stop someone today.",
         _html_table(open_wos, ["work_order_number", "party_name", "product",
                                "material", "boq_unit", "stock_unit"])),
        ("B. No action needed",
         "The units differ but conversion resolves them — for example Meter against m, "
         "or g against Kg.",
         _html_table(benign, ["product", "material", "boq_unit", "stock_unit", "boq_lines"])),
        ("C. BOQ materials with no matching inventory record",
         "BOQ lines are linked to materials by name, not by ID, so a typo or a rename "
         "breaks the link with no error. Work-order completion refuses these too.",
         _html_table(data["orphan_boq"], ["product", "material", "boq_unit", "boq_lines"])),
        ("D. Purchase-order lines whose unit cannot convert",
         "A goods receipt still records the quantity — the goods physically arrived — but "
         "without conversion, and now shows a warning on screen. These stock figures "
         "need review.",
         _html_table(po_blocking, ["purchase_number", "material", "po_unit", "stock_unit"])),
        ("E. Material names differing only by case or whitespace",
         "Because the name is the join key, these behave as separate materials in some "
         "places and the same one in others.",
         _html_table(data["near_dupe_materials"], ["normalized", "spellings", "rows"])),
        ("F. Negative stock",
         "A negative balance means more was consumed than was ever received.",
         _html_table(data["negative_stock"], ["name", "length_weight_nos", "unit"])),
        ("G. Blank units", "No unit recorded, so nothing can be converted.",
         _html_table(data["blank_units"], ["tbl", "item", "rows"])),
        ("H. Units with stray whitespace",
         "Conversion tolerates these, but equality checks and dropdowns do not.",
         _html_table(data["whitespace_units"], ["source", "unit"])),
        ("I. Every unit spelling currently in use", "The full vocabulary, per column.",
         _html_table(data["unit_usage"], ["source", "unit", "rows"])),
    ]
    body = "".join(
        f"<section><h2>{html.escape(t)}</h2><p>{html.escape(d)}</p>{tbl}</section>"
        for t, d, tbl in sections
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unit Discrepancy Report</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body,h1,h2,p,table{{margin:0}}
:root{{--paper:#F1F3F5;--surface:#fff;--surface-2:#E8EBEE;--ink:#171E26;
--ink-2:#4A5560;--ink-3:#6E7A86;--rule:#D3D9DE;--steel:#2B6C8F;
--oxide:#AF3B35;--amber:#8E5D0E;--moss:#3A6549}}
@media(prefers-color-scheme:dark){{:root:not([data-theme="light"]){{
--paper:#0F141A;--surface:#161D25;--surface-2:#1D2731;--ink:#E4E9ED;
--ink-2:#A4B0BA;--ink-3:#7B8794;--rule:#29343E;--steel:#72B2D2;
--oxide:#E69089;--amber:#DDA449;--moss:#82BA96}}}}
:root[data-theme="dark"]{{--paper:#0F141A;--surface:#161D25;--surface-2:#1D2731;
--ink:#E4E9ED;--ink-2:#A4B0BA;--ink-3:#7B8794;--rule:#29343E;--steel:#72B2D2;
--oxide:#E69089;--amber:#DDA449;--moss:#82BA96}}
body{{background:var(--paper);color:var(--ink);font:16px/1.6 ui-sans-serif,
-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:64rem;margin:0 auto;padding:3rem 1.5rem 5rem}}
h1{{font-size:clamp(1.7rem,4vw,2.4rem);font-weight:700;letter-spacing:-.02em}}
h2{{font-size:1.1rem;font-weight:650;letter-spacing:-.01em;margin-bottom:.4rem}}
header{{border-bottom:2px solid var(--ink);padding-bottom:1.2rem;margin-bottom:2rem}}
header p{{color:var(--ink-2);max-width:62ch;margin-top:.7rem}}
section{{margin-top:2.5rem}}
section>p{{color:var(--ink-2);font-size:.92rem;max-width:74ch;margin-bottom:.9rem}}
.score{{display:grid;gap:1px;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));
background:var(--rule);border:1px solid var(--rule);border-radius:8px;overflow:clip}}
.score>div{{background:var(--surface);padding:.9rem 1rem}}
.n{{display:block;font:600 1.6rem/1 ui-monospace,"SF Mono",Menlo,monospace;
font-variant-numeric:tabular-nums;margin-bottom:.35rem}}
.n-bad{{color:var(--oxide)}}.n-warn{{color:var(--amber)}}.n-ok{{color:var(--moss)}}
.l{{display:block;font-size:.74rem;color:var(--ink-3);line-height:1.35}}
.scroll{{overflow-x:auto;border:1px solid var(--rule);border-radius:6px}}
table{{border-collapse:collapse;width:100%;font-size:.85rem;background:var(--surface)}}
th,td{{padding:.55rem .8rem;text-align:left;border-bottom:1px solid var(--rule);
vertical-align:top;white-space:nowrap}}
th{{background:var(--surface-2);font:600 .67rem/1.4 ui-monospace,"SF Mono",Menlo,monospace;
text-transform:uppercase;letter-spacing:.08em;color:var(--ink-2)}}
tbody tr:last-child td{{border-bottom:none}}
td{{font-variant-numeric:tabular-nums}}
.none{{color:var(--moss);font-size:.88rem;font-style:italic}}
@media print{{:root{{--paper:#fff;--surface:#fff;--surface-2:#f2f2f2;--ink:#000;
--ink-2:#222;--ink-3:#555;--rule:#bbb}} .wrap{{max-width:none;padding:0}}
section,.scroll{{break-inside:avoid}}}}
</style></head><body><div class="wrap">
<header>
<h1>Unit &amp; material-link discrepancies</h1>
<p>BOQ lines, purchase-order lines and materials are linked by <strong>name</strong>
rather than by ID, and each carries its own free-text unit. Two things can go wrong
silently: a name that matches nothing, so consumption is never recorded; or two units
that measure different things, so the quantity is deducted in the wrong unit. Neither
raises an error on its own. This is every instance, grouped by what to do about it.</p>
</header>
<div class="score">{cards}</div>
{body}
</div></body></html>"""


if __name__ == "__main__":
    main()
