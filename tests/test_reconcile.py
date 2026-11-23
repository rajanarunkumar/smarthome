from datetime import date, timedelta

from hub.reconcile import days_needed, part_of, reconcile_report

# Made-up bill in the "24" layout: 30 days, 3,000 kWh, 1,500 free; 20¢ energy, 7¢ delivery, 4% fees, $10 base.
def bill(start="2026-06-01", days=30, kwh=3000.0, free=1500.0):
    end = (date.fromisoformat(start) + timedelta(days=days)).isoformat()
    energy = kwh * 0.20
    sub = 10 + energy - free * 0.20 + kwh * 0.07
    return {"period_start": start, "period_end": end, "days": days, "kwh": kwh, "free_kwh": free, "paid_kwh": kwh - free,
            "rate": 0.20, "plan": "Test", "base": 10.0, "energy": energy, "free_credit": free * 0.20, "tdu": kwh * 0.07,
            "tdu_credit": 0.0, "grt": round(sub * 0.04, 2), "tax": 0.0, "misc": 0.0, "total": round(sub * 1.04, 2)}


def meter(start, days, night=6.25, day=6.25):
    """8 night hours of `night` kWh and 16 paid hours of `day` kWh, each split into quarters."""
    iv = {}
    d0 = date.fromisoformat(start)
    for d in range(days):
        for h in range(24):
            for q in range(4):
                iv[f"{(d0 + timedelta(days=d)).isoformat()}T{h:02d}:{q * 15:02d}"] = (night if part_of(h) == "night" else day) / 4
    return iv


def test_parts_of_day():
    assert [part_of(h) for h in (3, 4, 11, 12, 15, 16, 19, 20)] == ["night", "morning", "morning", "afternoon",
                                                                     "afternoon", "evening", "evening", "night"]


def test_rebuilt_total_matches_bill():
    b = bill()
    iv = meter(b["period_start"], 30, night=6.25, day=3.125)   # 50 kWh a night, 50 kWh in the day
    r = reconcile_report([b], iv)["bills"][0]
    assert r["complete"] and r["meter_kwh"] == 3000 and r["meter_free_kwh"] == 1500
    assert abs(r["difference"]) < 0.05
    night = r["parts"][0]
    assert night["energy"] == 0 and night["delivery"] == round(1500 * 0.07, 2)
    assert r["parts"][1]["cents_per_kwh"] == round(27 * 1.04, 1)


def test_partial_meter_has_no_difference():
    b = bill()
    r = reconcile_report([b], meter(b["period_start"], 10))["bills"][0]
    assert not r["complete"] and r["difference"] is None


def test_year_over_year_saving_at_todays_prices():
    old, new = bill("2025-06-01"), bill("2026-06-01", kwh=2700.0)
    iv = {**meter(old["period_start"], 30, night=6.25, day=3.125), **meter(new["period_start"], 30, night=6.25, day=2.5)}
    rep = reconcile_report([old, new], iv)
    y = rep["bills"][1]["vs_last_year"]
    # 0.625 kWh less per paid hour × 16 h × 30 days = 300 kWh at 27¢ + 4%
    assert abs(y["saved"] - 300 * 0.27 * 1.04) < 0.05
    assert rep["savings"]["months"] == 1


def test_days_needed():
    b = bill("2026-06-01")
    need = days_needed([b], {"2026-06-02"}, today=date(2026, 6, 5))
    assert need == ["2026-06-01", "2026-06-03", "2026-06-04"]
