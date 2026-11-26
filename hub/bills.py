"""TXU bills: the ground truth for what the house is charged.

Reads the PDF bills you download from txu.com (two layouts so far: "Free Nights &
Solar Days 12", which also discounted delivery at night, and "Free Nights & Solar
Days 24", which doesn't). Each bill becomes one row in hub.db with its kWh, the
free-night kWh, and every charge, so the hub can:

- show the real cost per kWh (paid hours, free hours) from your own bills;
- set the prices used for the 15-minute cost estimates (energy, delivery, base);
- check the meter data against what TXU billed.

Import: python -m hub.bills import ~/Downloads/"TXU Invoices"   (or POST /api/bills/import)
The bills hold your name, address and account number; only the numbers below are kept.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    return float(m.group(1).replace(",", "")) if m else None


def _date(s: str | None) -> str | None:
    return datetime.strptime(s, "%m/%d/%Y").date().isoformat() if s else None


def parse_bill(text: str) -> dict | None:
    """One TXU bill's text -> its numbers. None if it doesn't look like a TXU electricity bill."""
    meter = re.search(r"\n\S+ (\d+) (?:Actual|Estimated) (\d\d/\d\d/\d{4}) \d+ (\d\d/\d\d/\d{4}) \d+ ([\d,]+) \d+ ([\d,]+)", text)
    if not meter or "ONCOR" not in text:
        return None
    kwh = float(meter.group(5).replace(",", ""))
    plan = re.search(r"\n(TXU Energy [^\n]*?)(?:SM)?\nBase Charge", text)
    invoice = re.search(r"Invoice Date: (\d\d/\d\d/\d{4})", text)
    row = {
        "invoice_date": _date(invoice.group(1) if invoice else None),
        "period_start": _date(meter.group(2)), "period_end": _date(meter.group(3)), "days": int(meter.group(1)),
        "kwh": kwh, "plan": plan.group(1).strip() if plan else None,
        "base": _num(r"Base Charge \$ ([\d.,]+)", text) or 0.0,
        "tdu": _num(r"ONCOR TDU Delivery Charges \$ ([\d.,]+)", text) or 0.0,
        "grt": _num(r"Gross Receipts Reimb \$ ([\d.,]+)", text) or 0.0,
        "tax": _num(r"Sales Tax \$ ([\d.,]+)\s*\nService Address", text) or 0.0,
        "misc": -(_num(r"Total Miscellaneous Charges \$ ([\d.,]+)CR", text) or 0.0),
        "total": _num(r"Current Charges \$ ([\d.,]+)", text),
    }
    if (m := re.search(r"Energy Charge \(([\d,]+) kWh x \$([\d.]+)\) \$ ([\d.,]+)", text)):   # "24" layout
        row["rate"] = float(m.group(2))
        row["energy"] = float(m.group(3).replace(",", ""))
        row["free_kwh"] = _num(r"Free Nights Savings \(([\d,]+) kWh", text) or 0.0
        row["free_credit"] = _num(r"Free Nights Savings \([\d,]+ kWh x \$[\d.]+\) \$ ([\d.,]+) CR", text) or 0.0
        row["tdu_credit"] = 0.0
    elif (m := re.search(r"Nights \(([\d,]+) kWh x \$([\d.]+)\) \$ ([\d.,]+)", text)):          # "12" layout
        day = re.search(r"Solar Days \(([\d,]+) kWh x \$([\d.]+)\) \$ ([\d.,]+)", text)
        row["rate"] = float(m.group(2))
        row["free_kwh"] = float(m.group(1).replace(",", ""))
        row["energy"] = float(m.group(3).replace(",", "")) + (float(day.group(3).replace(",", "")) if day else 0.0)
        row["free_credit"] = _num(r"Nights Discount \$ ([\d.,]+) CR", text) or 0.0
        row["tdu_credit"] = _num(r"Nights TDU Discount \$ ([\d.,]+) CR", text) or 0.0
    else:
        return None
    row["paid_kwh"] = row["kwh"] - row["free_kwh"]
    return row


def summarize(row: dict) -> dict:
    """Effective prices from one bill."""
    kwh, paid = row["kwh"], row["paid_kwh"]
    fees = (row["grt"] + row["tax"]) / max(row["total"] - row["grt"] - row["tax"], 1)  # share added on top
    delivery_c = (row["tdu"] - row["tdu_credit"]) / kwh * 100
    return {**row,
            "energy_net": round(row["energy"] - row["free_credit"], 2),
            "delivery_net": round(row["tdu"] - row["tdu_credit"], 2),
            "delivery_cents": round(delivery_c, 2),
            "tdu_cents_gross": round(row["tdu"] / kwh * 100, 2),
            "free_share": round(row["free_kwh"] / kwh, 3) if kwh else None,
            "fees_pct": round(fees * 100, 2),
            "all_in_cents": round(row["total"] / kwh * 100, 2),
            "paid_hour_cents": round((row["rate"] * 100 + row["tdu"] / kwh * 100) * (1 + fees), 2),
            "free_hour_cents": round(night_delivery_cents(row) * (1 + fees), 2)}


def night_delivery_cents(row: dict) -> float:
    """Delivery per kWh in the free window: the average, less any night delivery discount."""
    gross = row["tdu"] / row["kwh"] * 100
    if row["tdu_credit"] and row["free_kwh"]:
        return max(gross - row["tdu_credit"] / row["free_kwh"] * 100, 0.0)
    return gross


class BillStore:
    def __init__(self, db) -> None:
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS bills (
            period_end TEXT PRIMARY KEY, period_start TEXT, invoice_date TEXT, days INTEGER, kwh REAL,
            free_kwh REAL, paid_kwh REAL, rate REAL, plan TEXT, base REAL, energy REAL, free_credit REAL,
            tdu REAL, tdu_credit REAL, grt REAL, tax REAL, misc REAL, total REAL)""")
        db.commit()

    def put(self, row: dict) -> None:
        cols = ["period_end", "period_start", "invoice_date", "days", "kwh", "free_kwh", "paid_kwh", "rate", "plan", "base",
                "energy", "free_credit", "tdu", "tdu_credit", "grt", "tax", "misc", "total"]
        self.db.execute(f"INSERT OR REPLACE INTO bills ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [row.get(c) for c in cols])
        self.db.commit()

    def all(self) -> list[dict]:
        cur = self.db.execute("SELECT * FROM bills ORDER BY period_end")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def latest(self) -> dict | None:
        rows = self.all()
        return rows[-1] if rows else None


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader  # only needed for importing
    return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)


def import_folder(store: BillStore, folder: Path) -> dict:
    added, skipped = [], []
    for f in sorted(Path(folder).expanduser().glob("*.pdf")):
        try:
            row = parse_bill(pdf_text(f))
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{f.name}: {exc}")
            continue
        if row:
            store.put(row)
            added.append(row["period_end"])
        else:
            skipped.append(f"{f.name}: not a TXU electricity bill")
    return {"imported": len(added), "periods": sorted(added), "skipped": skipped}


def tariff_from(row: dict) -> dict:
    """Prices for the 15-minute estimates, from the latest bill."""
    s = summarize(row)
    free_delivery = round(night_delivery_cents(row), 3)
    return {"energy_cents": round(row["rate"] * 100, 3), "delivery_cents": s["tdu_cents_gross"],
            "night_delivery_cents": free_delivery, "monthly_fee": row["base"], "fees_pct": s["fees_pct"],
            "from_bill": row["period_end"], "plan": row["plan"]}


def bills_report(rows: list[dict]) -> dict:
    out = [summarize(r) for r in rows]
    recs = []
    if out:
        last = out[-1]
        year = out[-12:]
        total = sum(r["total"] for r in year)
        kwh = sum(r["kwh"] for r in year)
        recs.append({"level": "good", "title": f"Last {len(year)} bills: ${total:,.0f} for {kwh:,.0f} kWh",
                     "text": f"{total / kwh * 100:.1f}¢ per kWh all-in. Latest: {last['kwh']:,.0f} kWh, "
                             f"{last['free_share']:.0%} in free nights, ${last['total']:,.2f}."})
        tdu_year = sum(r["delivery_net"] for r in year)
        recs.append({"level": "warning", "title": f"Delivery cost ${tdu_year:,.0f} over {len(year)} bills",
                     "text": f"Oncor delivery is {last['tdu_cents_gross']:.1f}¢ on every kWh, night or day, on your current plan. "
                             "Every kWh you don't use at night still saves that."})
        old = [r for r in out if r["tdu_credit"]]
        if old and not last["tdu_credit"]:
            credit = sum(r["tdu_credit"] for r in old)
            recs.append({"level": "warning", "title": "Your previous plan also discounted night delivery",
                         "text": f"“{old[-1]['plan']}” took ${credit:,.0f} off delivery over {len(old)} bills "
                                 f"(about ${credit / len(old):,.0f} a month). The current plan doesn't. "
                                 "Worth comparing when the contract allows; check the early-termination fee first."})
        paid = [r for r in year if r["paid_kwh"]]
        if paid:
            peak = max(paid, key=lambda r: r["paid_kwh"])
            recs.append({"level": "good", "title": f"Most paid-hour use: {peak['period_end'][:7]}, {peak['paid_kwh']:,.0f} kWh",
                         "text": f"At {peak['rate'] * 100:.1f}¢ + delivery. Summer afternoons are where pre-cooling after 8 PM "
                                 "and a warmer daytime setpoint save most."})
    return {"bills": out, "recommendations": recs}


if __name__ == "__main__":  # python -m hub.bills import FOLDER
    if len(sys.argv) == 3 and sys.argv[1] == "import":
        from .config import data_dir
        import sqlite3
        db = sqlite3.connect(data_dir() / "hub.db")
        print(import_folder(BillStore(db), Path(sys.argv[2])))
    else:
        print(__doc__)
