"""Shared query helpers, CSV export, and hand-rolled SVG charting used
across blueprints. No external charting library / CDN -- everything here
renders as plain inline SVG so the app has zero internet dependency.
"""
import csv
import io
from flask import Response
import db


# ---------------------------------------------------------------------------
# Lookups used by many forms
# ---------------------------------------------------------------------------

def species_options(enabled_only=True):
    if enabled_only:
        return db.query("SELECT * FROM species WHERE enabled=1 ORDER BY name")
    return db.query("SELECT * FROM species ORDER BY name")


def breed_options(species_id=None):
    if species_id:
        return db.query("SELECT * FROM breeds WHERE species_id=? ORDER BY name", (species_id,))
    return db.query("SELECT * FROM breeds ORDER BY name")


def site_options():
    return db.query("SELECT * FROM sites WHERE active=1 ORDER BY name")


def group_options():
    return db.query("SELECT g.*, s.name AS site_name FROM groups_ g LEFT JOIN sites s ON s.id=g.site_id WHERE g.active=1 ORDER BY g.name")


def user_options():
    return db.query("SELECT * FROM users WHERE active=1 ORDER BY full_name")


def animal_label(row):
    if row is None:
        return "—"
    name = f" “{row['name']}”" if row["name"] else ""
    return f"{row['tag_id']}{name}"


def animal_lookup(animal_id):
    return db.query("SELECT * FROM animals WHERE id=?", (animal_id,), one=True)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow(r)
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


def xlsx_response(filename, header, rows, sheet_name="Sheet1"):
    try:
        from openpyxl import Workbook
    except ImportError:
        return csv_response(filename.replace(".xlsx", ".csv"), header, rows)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    for col_cells in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 40)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    resp = Response(bio.read(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


# ---------------------------------------------------------------------------
# Minimal inline-SVG charts (no external JS charting library)
# ---------------------------------------------------------------------------

def line_chart_svg(points, width=560, height=200, color="#1c6238", y_label="", x_labels=None):
    """points: list of (label, value). Renders a simple line+area chart."""
    pad_l, pad_r, pad_t, pad_b = 42, 16, 16, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    if not points:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">' \
               f'<text x="{width/2}" y="{height/2}" text-anchor="middle" fill="#66756a" font-size="13">No data yet</text></svg>'
    values = [p[1] for p in points]
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    span = vmax - vmin
    n = len(points)
    step = plot_w / max(n - 1, 1)

    def x_at(i):
        return pad_l + step * i

    def y_at(v):
        return pad_t + plot_h - ((v - vmin) / span) * plot_h

    coords = [(x_at(i), y_at(v)) for i, (_, v) in enumerate(points)]
    path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = path + f" L {coords[-1][0]:.1f},{pad_t+plot_h:.1f} L {coords[0][0]:.1f},{pad_t+plot_h:.1f} Z"

    gridlines = ""
    for gy in range(3):
        yy = pad_t + plot_h * gy / 2
        val = vmax - span * gy / 2
        gridlines += f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width-pad_r}" y2="{yy:.1f}" stroke="#dfe6e0" stroke-width="1"/>'
        gridlines += f'<text x="{pad_l-6}" y="{yy+4:.1f}" text-anchor="end" font-size="10" fill="#66756a">{val:.0f}</text>'

    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}"/>' for x, y in coords)

    labels = ""
    show_every = max(1, n // 6)
    for i, (lab, _) in enumerate(points):
        if i % show_every == 0 or i == n - 1:
            labels += f'<text x="{x_at(i):.1f}" y="{height-8}" text-anchor="middle" font-size="9.5" fill="#66756a">{lab}</text>'

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img">'
        f'{gridlines}'
        f'<path d="{area}" fill="{color}" fill-opacity="0.12" stroke="none"/>'
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>'
        f'{dots}{labels}'
        f'</svg>'
    )


def bar_chart_svg(bars, width=560, height=220, color="#2f9457"):
    """bars: list of (label, value)."""
    pad_l, pad_r, pad_t, pad_b = 42, 16, 16, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    if not bars:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">' \
               f'<text x="{width/2}" y="{height/2}" text-anchor="middle" fill="#66756a" font-size="13">No data yet</text></svg>'
    vmax = max((v for _, v in bars), default=0) or 1
    n = len(bars)
    gap = 10
    bw = max((plot_w - gap * (n - 1)) / n, 6)

    bars_svg = ""
    for i, (lab, v) in enumerate(bars):
        bh = (v / vmax) * plot_h if vmax else 0
        x = pad_l + i * (bw + gap)
        y = pad_t + plot_h - bh
        bars_svg += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" fill="{color}"/>'
        bars_svg += f'<text x="{x+bw/2:.1f}" y="{pad_t+plot_h+14}" text-anchor="middle" font-size="9.5" fill="#66756a">{lab}</text>'
        if v:
            bars_svg += f'<text x="{x+bw/2:.1f}" y="{y-4:.1f}" text-anchor="middle" font-size="9.5" fill="#1b241d">{v:g}</text>'

    gridline = f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{width-pad_r}" y2="{pad_t+plot_h}" stroke="#dfe6e0" stroke-width="1"/>'
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img">'
        f'{gridline}{bars_svg}</svg>'
    )
