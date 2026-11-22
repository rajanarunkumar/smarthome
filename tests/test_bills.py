import sqlite3

from hub.bills import BillStore, bills_report, parse_bill, summarize, tariff_from

HEAD = """Invoice Date: 11/12/2026
Meter ID Days In Reading Read Type Previous Read Date Previous Meter Read Current Read Date Current Meter Read Usage (kWh) Multiplier Billed Usage (kWh)
X1 30 Actual 10/01/2026 1000 10/31/2026 3000 2000 1 2000
"""
NEW = HEAD + """TXU Energy Free Nights & Solar Days 24 (8 pm)SM
Base Charge $ 9.95
Energy Charge (2000 kWh x $0.22800000) $ 456.00
Free Nights Savings (1000 kWh x $0.22800000) $ 228.00 CR
Subtotal $ 237.95
TDU Charges and Other Fees
ONCOR TDU Delivery Charges $ 140.00
Gross Receipts Reimb $ 7.56
Subtotal $ 147.56
Sales Tax $ 7.71
Service Address Charges Subtotal $ 393.22
Total Miscellaneous Charges $ 1.00CR
Current Charges $ 392.22
ONCOR ELECTRIC DELIVERY COMPANY
"""
OLD = HEAD + """TXU Energy Free Nights & Solar Days 12 (8 pm)SM
Base Charge $ 9.95
Solar Days (1000 kWh x $0.25000000) $ 250.00
Nights (1000 kWh x $0.25000000) $ 250.00
Nights Discount $ 250.00 CR
Nights TDU Discount $ 50.00 CR
Subtotal $ 209.95
TDU Charges and Other Fees
ONCOR TDU Delivery Charges $ 120.00
Gross Receipts Reimb $ 6.00
Subtotal $ 126.00
Sales Tax $ 6.00
Service Address Charges Subtotal $ 341.95
Current Charges $ 341.95
ONCOR ELECTRIC DELIVERY COMPANY
"""


def adds_up(r):
    calc = r["base"] + r["energy"] - r["free_credit"] - r["tdu_credit"] + r["tdu"] + r["grt"] + r["tax"] + r["misc"]
    return abs(calc - r["total"]) < 0.02


def test_current_plan_layout():
    r = parse_bill(NEW)
    assert (r["kwh"], r["free_kwh"], r["paid_kwh"], r["rate"], r["tdu"], r["tax"], r["misc"]) == (2000, 1000, 1000, 0.228, 140.0, 7.71, -1.0)
    assert r["period_end"] == "2026-10-31" and adds_up(r)
    t = tariff_from(r)
    assert t["energy_cents"] == 22.8 and t["delivery_cents"] == 7.0 and t["night_delivery_cents"] == 7.0


def test_old_plan_with_night_delivery_discount():
    r = parse_bill(OLD)
    assert r["free_kwh"] == 1000 and r["tdu_credit"] == 50.0 and adds_up(r)
    assert summarize(r)["free_hour_cents"] < summarize(r)["paid_hour_cents"]
    assert tariff_from(r)["night_delivery_cents"] == 1.0     # 6¢ average less 5¢ off each night kWh


def test_not_a_bill():
    assert parse_bill("hello") is None


def test_store_and_report():
    store = BillStore(sqlite3.connect(":memory:"))
    old = {**parse_bill(OLD), "period_end": "2026-01-31"}
    store.put(old)
    store.put(parse_bill(NEW))
    assert store.latest()["period_end"] == "2026-10-31"
    titles = [x["title"] for x in bills_report(store.all())["recommendations"]]
    assert any("previous plan" in t for t in titles)
