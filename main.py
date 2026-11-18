#!/usr/bin/env python3
"""gta6-twitch-ccu-forecast-01 entry: forecast GTA VI's launch-day Twitch
viewers, hour by hour, as five quantiles per hour.

Method
------
The case hands us hour 1's printed viewers (p1), the pre-launch history,
and a library of comparable past launches, each with its own hourly curve
over its release day. A launch day's *shape* — the curve divided by its own
hour 1 — is far more stable across games than its level: the same regional
midnight unlocks roll west (New Zealand, Europe, the UK, then the US), the
same morning dip and prime-time build follow. So:

1. For every comparable whose hour 1 is big enough to be signal rather
   than noise, compute the ratio curve r_i(h) = viewers_i(h) / viewers_i(1)
   for h = 2..24 of its release day.
2. Weight each comparable by how close its hour-1 audience is to this
   case's p1 (a Gaussian kernel on the log size ratio): a launch that
   opened at GTA VI's scale shapes the forecast more than a tiny one.
3. For each hour, take the weighted empirical quantiles of the ratios and
   blend them, in log space, with a lognormal centred on the weighted
   median ratio whose spread is that hour's own interquartile range —
   the empirical tails of ~35 launches are noisy, the blend keeps the
   5% and 95% quantiles calibrated rather than jumpy.
4. Multiply by p1. Hours with too few comparables fall back to a
   persistence median with a random-walk spread, like the house baseline
   but with the spread the comparable library shows at that hour.

Standard library only, no network, deterministic: the same case always
produces the same bytes.
"""

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.6448536269514722, -0.6744897501960817, 0.0, 0.6744897501960817, 1.6448536269514722]
MIN_HOUR1 = 500.0        # a comparable's hour 1 below this is mostly noise
MIN_PER_HOUR = 6         # ratios needed before an hour trusts the library
KERNEL_BW = 1.5          # log-size bandwidth of the similarity kernel
EMPIRICAL_SHARE = 0.55   # blend weight on the empirical quantiles (log space)


def round2(x: float):
    """Round to 2 decimals, half up; whole numbers come back as int."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if float(value).is_integer() else value


def parse_ts(text: str) -> float:
    """ISO-8601 'Z' timestamp to epoch seconds (stdlib only)."""
    from datetime import datetime, timezone

    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def hour_ratios(comp: dict):
    """One comparable's release-day ratio curve: {hour: viewers(hour)/viewers(1)}.

    Returns (ratios, hour1_viewers) or None when the comparable cannot
    anchor a curve (no day_start, no hour 1, hour 1 too small).
    """
    try:
        start = parse_ts(comp["day_start"])
        by_hour = {}
        for point in comp.get("hourly") or []:
            stamp = point.get("from") or point.get("at")
            viewers = point.get("viewers")
            if stamp is None or viewers is None:
                continue
            idx = int(round((parse_ts(stamp) - start) / 3600.0)) + 1
            if 1 <= idx <= 24 and viewers > 0:
                by_hour[idx] = float(viewers)
    except (KeyError, TypeError, ValueError):
        return None
    first = by_hour.get(1)
    if not first or first < MIN_HOUR1 or len(by_hour) < 12:
        return None
    return {h: v / first for h, v in by_hour.items() if h >= 2}, first


def weighted_quantile(pairs, tau: float) -> float:
    """pairs: [(value, weight)] -> the tau weighted quantile (lower method)."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return ordered[len(ordered) // 2][0]
    target = tau * total
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= target:
            return value
    return ordered[-1][0]


def hour_forecast(p1: float, h: int, ratios_by_hour: dict) -> list:
    """The five quantiles of hour h's viewers, as numbers > 0."""
    pairs = ratios_by_hour.get(h) or []
    if len(pairs) >= MIN_PER_HOUR:
        logs = [(math.log(max(r, 1e-6)), w) for r, w in pairs]
        med = weighted_quantile(pairs, 0.5)
        q25 = weighted_quantile(logs, 0.25)
        q75 = weighted_quantile(logs, 0.75)
        sigma = (q75 - q25) / 1.349
        sigma = min(max(sigma, 0.08), 1.20)
        out = []
        for tau, z in zip(LEVELS, Z):
            emp = weighted_quantile(pairs, tau)
            logn = med * math.exp(z * sigma)
            blended = math.exp(EMPIRICAL_SHARE * math.log(max(emp, 1e-6))
                               + (1 - EMPIRICAL_SHARE) * math.log(max(logn, 1e-6)))
            out.append(blended)
    else:
        # Too little library for this hour: persistence median, spread
        # widening like a random walk's, as the house baseline does.
        sigma = 0.15 * math.sqrt(h - 1)
        out = [math.exp(z * sigma) for z in Z]
    qs = sorted(p1 * r for r in out)
    return [round2(min(max(q, 1.0), 1e9)) for q in qs]


def forecast_case(case: dict, name: str) -> dict:
    if case["levels"] != LEVELS:
        raise ValueError(f"{name} asks for levels {case['levels']}, not {LEVELS}")
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = max(1.0, float(first["viewers"]))

    ratios_by_hour: dict[int, list] = {}
    for comp in case.get("comparables") or []:
        got = hour_ratios(comp)
        if got is None:
            continue
        ratios, first_viewers = got
        weight = math.exp(-0.5 * (math.log(first_viewers / p1) / KERNEL_BW) ** 2)
        for h, r in ratios.items():
            # Winsorise: one wild comparable (a raid, an outage, a mistimed
            # regional unlock) must not drag an hour's tail on its own.
            r = min(max(r, 0.02), 30.0)
            ratios_by_hour.setdefault(h, []).append((r, weight))

    return {"hours": [{"hour": row["hour"], "q": hour_forecast(p1, row["hour"], ratios_by_hour)}
                      for row in case["forecast"]]}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
