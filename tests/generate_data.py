"""Synthetic recruitment funnel data for a portfolio showcase dashboard.

Grain: one row per application. Twelve months to 2026-09-19.
No real candidate, client or company data is involved.

Narrative deliberately planted so the dashboard has something to say:
  * Job Board is the largest source by volume and the weakest by hire rate.
  * Referral is the smallest meaningful source and the strongest converter.
  * Interview -> Offer is the tightest stage in the funnel.
  * Time-to-hire drifts upward for Senior/Lead roles over the year.
"""

import os

import numpy as np
import pandas as pd

SEED = 20260919
rng = np.random.default_rng(SEED)

END = pd.Timestamp("2026-09-19")
START = END - pd.DateOffset(months=12)

# source -> (share of applications, screen rate, interview rate, offer rate, accept rate)
SOURCES = {
    "Referral":          (0.09, 0.66, 0.58, 0.42, 0.88),
    "Recruiter Outreach":(0.17, 0.52, 0.44, 0.31, 0.74),
    "Agency":            (0.11, 0.47, 0.40, 0.29, 0.70),
    "LinkedIn":          (0.21, 0.38, 0.31, 0.22, 0.68),
    "Careers Site":      (0.08, 0.31, 0.26, 0.19, 0.76),
    "Job Board":         (0.34, 0.21, 0.15, 0.10, 0.62),
}

RECRUITERS = ["Anna K.", "Dmytro S.", "Olena P.", "Marek W.", "Sofia R.", "Tomas H."]
RECRUITER_SKILL = dict(zip(RECRUITERS, [1.14, 1.02, 1.09, 0.88, 0.95, 0.78]))

DEPARTMENTS = {
    "Engineering":       0.34,
    "Data & Analytics":  0.18,
    "QA":                0.14,
    "DevOps":            0.12,
    "Product":           0.12,
    "Design":            0.10,
}
SENIORITY = {"Junior": 0.26, "Mid": 0.38, "Senior": 0.27, "Lead": 0.09}
SENIORITY_DIFFICULTY = {"Junior": 0.88, "Mid": 1.00, "Senior": 1.18, "Lead": 1.42}
LOCATIONS = {"Remote EU": 0.31, "Kyiv": 0.24, "Warsaw": 0.19, "Berlin": 0.15, "Lisbon": 0.11}

# base placement fee by seniority, in EUR
FEE = {"Junior": 4200, "Mid": 6800, "Senior": 11500, "Lead": 17000}

N = 4300


def pick(mapping, n):
    keys = list(mapping)
    p = np.array([mapping[k] if not isinstance(mapping[k], tuple) else mapping[k][0] for k in keys], dtype=float)
    return rng.choice(keys, size=n, p=p / p.sum())


def main():
    # Applications arrive with a mild upward trend and a summer dip.
    days = (END - START).days
    offsets = rng.integers(0, days, size=N)
    applied = START + pd.to_timedelta(offsets, unit="D")

    df = pd.DataFrame({
        "application_id": [f"APP-{i:05d}" for i in range(1, N + 1)],
        "applied_date": applied,
        "source": pick(SOURCES, N),
        "recruiter": pick({r: 1 / len(RECRUITERS) for r in RECRUITERS}, N),
        "department": pick(DEPARTMENTS, N),
        "seniority": pick(SENIORITY, N),
        "location": pick(LOCATIONS, N),
    })
    df = df.sort_values("applied_date").reset_index(drop=True)

    month_index = ((df.applied_date.dt.year - START.year) * 12
                   + df.applied_date.dt.month - START.month)

    screen_p = np.array([SOURCES[s][1] for s in df.source])
    intv_p = np.array([SOURCES[s][2] for s in df.source])
    offer_p = np.array([SOURCES[s][3] for s in df.source])
    accept_p = np.array([SOURCES[s][4] for s in df.source])

    skill = np.array([RECRUITER_SKILL[r] for r in df.recruiter])
    difficulty = np.array([SENIORITY_DIFFICULTY[s] for s in df.seniority])

    screened = rng.random(N) < np.clip(screen_p * skill * (2 - difficulty) ** 0.35, 0.02, 0.95)
    interviewed = screened & (rng.random(N) < np.clip(intv_p * skill / difficulty ** 0.25 / screen_p * 0.62, 0.02, 0.95))
    offered = interviewed & (rng.random(N) < np.clip(offer_p / intv_p * 0.58 / difficulty ** 0.3, 0.02, 0.95))
    hired = offered & (rng.random(N) < accept_p)

    stage = np.where(hired, "Hired",
             np.where(offered, "Offer declined",
             np.where(interviewed, "Interviewed",
             np.where(screened, "Screened", "Applied"))))

    # Time to hire: base by seniority, drifting up over the year for senior roles.
    base = np.array([{"Junior": 22, "Mid": 29, "Senior": 41, "Lead": 55}[s] for s in df.seniority])
    drift = np.where(np.isin(df.seniority, ["Senior", "Lead"]), month_index * 0.85, month_index * 0.12)
    noise = rng.normal(0, 6, N)
    days_to_hire = np.where(hired, np.clip(base + drift + noise / skill, 8, 140).round(0), np.nan)

    fee = np.where(hired,
                   np.array([FEE[s] for s in df.seniority]) * rng.normal(1.0, 0.09, N),
                   np.nan)

    df["stage_reached"] = stage
    df["reached_screen"] = screened.astype(int)
    df["reached_interview"] = interviewed.astype(int)
    df["reached_offer"] = offered.astype(int)
    df["reached_hire"] = hired.astype(int)
    df["days_to_hire"] = days_to_hire
    df["placement_fee_eur"] = np.where(hired, fee.round(0), np.nan)
    df["applied_month"] = df.applied_date.values.astype("datetime64[M]")
    df["applied_date"] = df.applied_date.dt.strftime("%Y-%m-%d")
    df["applied_month"] = pd.to_datetime(df.applied_month).dt.strftime("%Y-%m-%d")

    out = os.environ.get("PBIMAKE_FIXTURE_OUT", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures",
        "hiring_funnel_clean.csv"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8")

    print(f"rows: {len(df):,}   -> {out}")
    print(f"date range: {df.applied_date.min()} .. {df.applied_date.max()}")
    print()
    print("FUNNEL")
    for label, col in [("Applications", None), ("Screened", "reached_screen"),
                       ("Interviewed", "reached_interview"), ("Offers", "reached_offer"),
                       ("Hires", "reached_hire")]:
        n = len(df) if col is None else int(df[col].sum())
        print(f"  {label:<14} {n:>6,}   {n/len(df):6.1%} of applications")
    print()
    print("BY SOURCE")
    g = df.groupby("source").agg(apps=("application_id", "count"),
                                 hires=("reached_hire", "sum")).sort_values("apps", ascending=False)
    g["app_share"] = g.apps / g.apps.sum()
    g["hire_share"] = g.hires / g.hires.sum()
    g["hire_rate"] = g.hires / g.apps
    print(g.to_string(formatters={"app_share": "{:.1%}".format,
                                  "hire_share": "{:.1%}".format,
                                  "hire_rate": "{:.1%}".format}))
    print()
    print("BY RECRUITER")
    r = df.groupby("recruiter").agg(apps=("application_id", "count"),
                                    hires=("reached_hire", "sum"),
                                    avg_tth=("days_to_hire", "mean"),
                                    fees=("placement_fee_eur", "sum")).sort_values("hires", ascending=False)
    r["hire_rate"] = r.hires / r.apps
    print(r.to_string(formatters={"hire_rate": "{:.1%}".format, "avg_tth": "{:.1f}".format,
                                  "fees": "{:,.0f}".format}))
    print()
    print(f"TOTAL FEES: EUR {df.placement_fee_eur.sum():,.0f}")
    print(f"AVG TIME TO HIRE: {df.days_to_hire.mean():.1f} days")


if __name__ == "__main__":
    main()
