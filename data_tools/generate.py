"""StashTrace synthetic data generator (single generator, parameterised scale, fixed seed).

    python -m data_tools.generate --profile full --seed 20261022 --out data/full
    python -m data_tools.generate --profile starter --out data/starter

How the data is built (masters first, then dependent events):
 1. Regions, stores/warehouses, SKUs, assortments (StoreSKU with effective dates and opening
    balances), employees + effective-dated role assignments (incl. leavers), shifts, promotions,
    POS-batch source documents.
 2. A per-store/SKU *physical* simulation in time order: sales (weekday + promotion seasonality,
    capped by what is physically on the shelf), replenishment receipts (vendor PO or DC transfer),
    customer returns (restocked or not), approved damage/expiry write-offs, small unrecorded shrink,
    cycle counts (physical quantity + miscount noise) and *planted* hidden theft.
 3. Warehouses are simulated from the transfer dispatches the stores requested.
 4. Reporting anomalies are injected into the event stream (wrong store, missing adjustment reason,
    expired role, feed replays, payload conflicts, POS re-rings as reversal+replacement), with
    corrections resubmitted for a share of them.
 5. Derived values: the generator recomputes the ledger that the backend should end up with, posts
    the store's COUNT_CORRECTION write-offs from it, and derives every ground-truth label from the
    planted cause (e.g. "hidden theft outstanding >= 5 units"), never by random assignment.
 6. Volume is topped up to the exact movement-input target with end-of-period sales after each
    pair's last count (so no label or reconciliation is affected).

Everything is deterministic for a given (profile, seed): each component draws from its own RNG
derived from the seed with SHA-256, and every output file is written in a fixed order.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import random
import sys
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from backend.app import catalog as C
from backend.app.services.posting import COUNT_FIELDS, MOVEMENT_FIELDS
from data_tools.masters import MASTER_FILES, write_csv

GENERATOR_VERSION = "1.0.0"
DEFAULT_SEED = 20261022
START = date(2026, 1, 1)
DAY = 86400
THRESHOLD = 5  # documented label rule: hidden loss >= 5 units is an unexplained-loss positive

PROFILES = {
    "tiny": dict(stores=6, warehouses=1, regions=1, skus=60, skus_per_store=(25, 40), movements=8_000,
                 counts=300, days=60, sale_fraction=0.835),
    "starter": dict(stores=9, warehouses=1, regions=1, skus=250, skus_per_store=(90, 140), movements=60_000,
                    counts=1_500, days=91, sale_fraction=0.835),
    "full": dict(stores=92, warehouses=8, regions=8, skus=5_000, skus_per_store=(430, 700), movements=1_000_000,
                 counts=20_000, days=181, sale_fraction=0.768),
}

# Planted scenario volumes as a fraction of the count / movement / transfer budgets (min 3 each).
SCENARIO_RATES = dict(
    theft=0.022, boundary_each=0.003, timing=0.012, late=0.012, invalid_count=0.002, missed_location=0.004,
    wrong_store=0.0006, missing_reason=0.0006, role_expired=0.00015, duplicate=0.002, conflict=0.0001,
    rering=0.0002, repeated_adj=0.004,
    transfer_double=0.006, transfer_unresolved=0.006, transfer_once=0.010, transfer_delay=0.03,
)

CITIES = ["Ashford", "Brookvale", "Cedar Point", "Dunmore", "Elmstead", "Fairhaven", "Glenrock", "Harbor View",
          "Ironwood", "Juniper Hill", "Kingsbridge", "Lakemont", "Maple Ridge", "Northgate", "Oakdale", "Pinecrest",
          "Queensbury", "Riverton", "Silverlake", "Thornbury", "Upton", "Valemont", "Westfield", "Yarrow", "Zephyr Bay"]
SUFFIX = ["Central", "Plaza", "Market", "Square", "Commons", "Crossing", "Park", "Station"]
TZ = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix",
      "America/Detroit", "America/Boise", "America/Indiana/Indianapolis"]
BRANDS = ["Nordvale", "Brightpath", "Kestrel", "Oakline", "Lumina", "Halden", "Verity", "Corvo", "Sable", "Truly",
          "Mistral", "Aurel", "Pebble & Co", "Fennic", "Quartzline", "Ridgeway"]
NOUNS = {
    "GROCERY": ["pasta", "rice", "cereal", "granola", "olive oil", "coffee beans", "tea bags", "crackers"],
    "BEVERAGE": ["sparkling water", "cold brew", "energy drink", "kombucha", "orange juice", "iced tea"],
    "SPIRITS": ["single malt", "small-batch gin", "aged rum", "reposado tequila", "vodka"],
    "HEALTH_BEAUTY": ["shampoo", "conditioner", "body wash", "sunscreen", "toothpaste", "vitamins"],
    "COSMETICS": ["lipstick", "mascara", "foundation", "eyeliner", "serum", "palette"],
    "ELECTRONICS": ["wireless earbuds", "power bank", "smart plug", "bluetooth speaker", "USB-C cable", "smartwatch"],
    "ACCESSORIES": ["sunglasses", "wallet", "phone case", "watch strap", "backpack"],
    "HOME": ["candle", "throw blanket", "storage box", "mug set", "towel set"],
    "TOYS": ["building set", "puzzle", "plush toy", "card game", "RC car"],
    "APPAREL": ["t-shirt", "hoodie", "socks 3-pack", "beanie", "leggings"],
    "FRESH": ["salad kit", "berries", "sourdough loaf", "yogurt", "cheese wedge"],
    "HARDWARE": ["LED bulb", "drill bit set", "tape measure", "batteries 8-pack", "extension cord"],
}
ADJ = ["classic", "premium", "everyday", "organic", "ultra", "mini", "family size", "pro", "lite", "deluxe"]
CAT_WEIGHT = {"GROCERY": 14, "BEVERAGE": 10, "SPIRITS": 4, "HEALTH_BEAUTY": 10, "COSMETICS": 8, "ELECTRONICS": 7,
              "ACCESSORIES": 6, "HOME": 8, "TOYS": 6, "APPAREL": 9, "FRESH": 9, "HARDWARE": 9}
CAT_COST = {"GROCERY": (350, 0.5), "BEVERAGE": (220, 0.4), "SPIRITS": (2200, 0.5), "HEALTH_BEAUTY": (600, 0.5),
            "COSMETICS": (900, 0.6), "ELECTRONICS": (2800, 0.8), "ACCESSORIES": (1500, 0.6), "HOME": (1200, 0.6),
            "TOYS": (1400, 0.6), "APPAREL": (1300, 0.5), "FRESH": (300, 0.4), "HARDWARE": (900, 0.6)}
CAT_RISK = {"SPIRITS": "HIGH", "COSMETICS": "HIGH", "ELECTRONICS": "HIGH", "ACCESSORIES": "MEDIUM",
            "HEALTH_BEAUTY": "MEDIUM", "TOYS": "MEDIUM", "APPAREL": "MEDIUM"}
PERISHABLE = {"FRESH", "GROCERY", "BEVERAGE"}
FIRST = ["Alex", "Blair", "Casey", "Dana", "Emery", "Finley", "Gray", "Harper", "Indy", "Jules", "Kai", "Logan",
         "Morgan", "Noel", "Oakley", "Parker", "Quinn", "Reese", "Sage", "Tatum", "Umi", "Val", "Wren", "Yael"]
LAST = ["Abernathy", "Brightwater", "Calloway", "Dunsmore", "Ellery", "Fairbanks", "Galloway", "Hollis", "Ingram",
        "Jessop", "Kendrick", "Lockhart", "Marlowe", "Northcott", "Oakes", "Pemberton", "Quill", "Radley",
        "Sterling", "Thorne", "Underhill", "Vance", "Whitlock", "Yardley"]


# ---------------------------------------------------------------- helpers
def rng_for(seed: int, *keys) -> random.Random:
    h = hashlib.sha256(("|".join([str(seed), *map(str, keys)])).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def hint(seed: int, *keys) -> int:
    return int.from_bytes(hashlib.sha256(("|".join([str(seed), *map(str, keys)])).encode()).digest()[:8], "big")


class Clock:
    def __init__(self, days: int):
        self.days = days
        self.day_str = [(START + timedelta(days=d)).isoformat() for d in range(days + 30)]
        self.end = days * DAY

    def iso(self, s: int) -> str:
        d, r = divmod(s, DAY)
        h, r = divmod(r, 3600)
        m, sec = divmod(r, 60)
        return f"{self.day_str[d]}T{h:02d}:{m:02d}:{sec:02d}Z"

    def date(self, s: int) -> str:
        return self.day_str[s // DAY]


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 40:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def at(day: int, hour: int, minute: int = 0, sec: int = 0) -> int:
    return day * DAY + hour * 3600 + minute * 60 + sec


class Ev:
    """One physical/reporting event of a store/SKU pair (compact; ~1M of these in the full profile)."""
    __slots__ = ("t", "rt", "mt", "q", "sq", "p", "x", "post", "mid", "sid", "sys", "rows")

    def __init__(self, t, mt, q, sq, p, x=None):
        self.t, self.mt, self.q, self.sq, self.p, self.x = t, mt, q, sq, p, x
        self.rt = None      # received_at seconds
        self.post = True    # final ledger effect present?
        self.mid = None     # movement_id
        self.sid = None     # (system, event id)
        self.sys = None
        self.rows = None    # extra emitted rows (anomalies)


@dataclass
class Pair:
    idx: int
    store: int
    sku: int
    active_from: int
    active_to: int | None
    opening: int
    repl: str
    wh: int | None
    rate: float = 0.0
    events: list = field(default_factory=list)
    counts: list = field(default_factory=list)
    thefts: list = field(default_factory=list)
    shrink: list = field(default_factory=list)
    promos: list = field(default_factory=list)
    final_physical: int = 0


# ----------------------------------------------------------------- generator
class Generator:
    def __init__(self, profile: str, seed: int):
        self.profile = profile
        self.cfg = dict(PROFILES[profile])
        self.seed = seed
        self.clock = Clock(self.cfg["days"])
        self.days = self.cfg["days"]
        self.stats: dict = defaultdict(int)
        self.gt: list[dict] = []

    def r(self, *keys) -> random.Random:
        return rng_for(self.seed, *keys)

    def n_scn(self, key: str, base: int) -> int:
        return max(3, int(round(SCENARIO_RATES[key] * base)))

    # ------------------------------------------------------------ masters
    def build_masters(self) -> None:
        cfg, rng = self.cfg, self.r("masters")
        n_st, n_wh, n_reg = cfg["stores"], cfg["warehouses"], cfg["regions"]
        self.regions = [f"R{i + 1:02d}" for i in range(n_reg)]
        self.stores = []
        for i in range(n_st + n_wh):
            is_wh = i >= n_st
            region = self.regions[(i - n_st) % n_reg] if is_wh else self.regions[i % n_reg]
            city = CITIES[(i * 7 + 3) % len(CITIES)]
            self.stores.append({
                "store_id": f"STORE-{i + 1:06d}", "type": "WAREHOUSE" if is_wh else "STORE", "region": region,
                "name": f"{city} {'Distribution Center' if is_wh else SUFFIX[i % len(SUFFIX)]}",
                "tz": TZ[self.regions.index(region) % len(TZ)],
                "opened": (date(2012, 1, 1) + timedelta(days=rng.randint(0, 4800))).isoformat(),
                "size": 1.0 if is_wh else rng.lognormvariate(0, 0.35),
            })
        self.store_idx = list(range(n_st))
        self.wh_of_region = {s["region"]: i for i, s in enumerate(self.stores) if s["type"] == "WAREHOUSE"}
        cats = list(CAT_WEIGHT)
        cw = [CAT_WEIGHT[c] for c in cats]
        self.skus = []
        ranks = list(range(cfg["skus"]))
        rng.shuffle(ranks)
        for i in range(cfg["skus"]):
            cat = rng.choices(cats, cw)[0]
            mu, sd = CAT_COST[cat]
            cost = max(25, int(rng.lognormvariate(math.log(mu), sd)))
            price = int(cost * rng.uniform(1.35, 2.4))
            self.skus.append({
                "sku_id": f"SKU-{i + 1:06d}", "cat": cat, "cost": cost, "price": price,
                "brand": BRANDS[rng.randrange(len(BRANDS))],
                "name": f"{rng.choice(ADJ)} {rng.choice(NOUNS[cat])}",
                "risk": CAT_RISK.get(cat, "LOW"),
                "status": "DISCONTINUED" if rng.random() < 0.02 else "ACTIVE",
                "introduced": (date(2023, 1, 1) + timedelta(days=rng.randint(0, 1000))).isoformat(),
                "pop": 1.0 / (ranks[i] + 1) ** 0.85,
            })
        # assortment (weighted sampling without replacement, Efraimidis-Spirakis)
        self.pairs: list[Pair] = []
        self.pair_of: dict[tuple[int, int], int] = {}
        lo, hi = cfg["skus_per_store"]
        for s in self.store_idx:
            r = self.r("assort", s)
            k = r.randint(lo, hi)
            keyed = sorted(((r.random() ** (1.0 / sk["pop"]), j) for j, sk in enumerate(self.skus)), reverse=True)
            for _, j in sorted(keyed[:k], key=lambda x: x[1]):
                u = r.random()
                af, at_ = 0, None
                if u < 0.04:
                    af = r.randint(self.days // 6, self.days * 3 // 5) * DAY
                elif u < 0.06:
                    at_ = r.randint(self.days // 2, self.days - 20) * DAY
                wh = self.wh_of_region.get(self.stores[s]["region"])
                repl = "DC" if (wh is not None and r.random() < 0.4) else "VENDOR"
                p = Pair(len(self.pairs), s, j, af, at_, 0, repl, wh if repl == "DC" else None)
                self.pair_of[(s, j)] = p.idx
                self.pairs.append(p)
        # warehouse assortments: every SKU its region's stores replenish from the DC
        for p in list(self.pairs):
            if p.repl == "DC" and (p.wh, p.sku) not in self.pair_of:
                wp = Pair(len(self.pairs), p.wh, p.sku, 0, None, 0, "VENDOR", None)
                self.pair_of[(p.wh, p.sku)] = wp.idx
                self.pairs.append(wp)
        self._build_people()
        self._build_promotions()

    def _build_people(self) -> None:
        rng = self.r("people")
        self.employees: list[dict] = []
        self.assignments: list[dict] = []

        def emp(name, etype, home=None, hired="2024-01-08", term=None):
            eid = f"EMP-{len(self.employees) + 1:06d}"
            self.employees.append({"employee_id": eid, "display_name": name, "employee_type": etype,
                                   "home_store_id": self.stores[home]["store_id"] if home is not None else None,
                                   "hired_on": hired, "terminated_on": term,
                                   "status": "TERMINATED" if term else "ACTIVE"})
            return eid

        def assign(eid, role, store, frm=0, to=None):
            self.assignments.append({
                "assignment_id": f"RA-{len(self.assignments) + 1:06d}", "employee_id": eid, "role_code": role,
                "store_id": self.stores[store]["store_id"] if store is not None else None,
                "effective_from": "2024-01-08T00:00:00Z" if frm == 0 else self.clock.iso(frm),
                "effective_to": self.clock.iso(to) if to is not None else None})

        def person():
            return f"{rng.choice(FIRST)} {rng.choice(LAST)}"

        self.svc_pos = emp("POS integration feed", "SERVICE")
        self.svc_wms = emp("WMS integration feed", "SERVICE")
        self.svc_sys = emp("StashTrace detection service", "SERVICE")
        for e in (self.svc_pos, self.svc_wms):
            assign(e, "SERVICE_FEED", None)
        assign(self.svc_sys, "SYSTEM", None)
        self.admin = emp(person(), "HUMAN")
        assign(self.admin, "ADMIN", None)
        self.ops = [emp(person(), "HUMAN") for _ in range(2)]
        for e in self.ops:
            assign(e, "OPS_MANAGER", None)
        self.analysts = [emp(person(), "HUMAN") for _ in range(2)]
        for e in self.analysts:
            assign(e, "ANALYST", None)
        self.lp = {}
        for reg in self.regions:
            e = emp(person(), "HUMAN")
            self.lp[reg] = e
            for i, s in enumerate(self.stores):
                if s["region"] == reg:
                    assign(e, "LOSS_PREVENTION", i)
        # store staff with effective-dated assignments; some clerks leave mid-period
        self.roster: dict[int, dict[str, list]] = {}
        self.leavers: dict[int, list] = defaultdict(list)
        for i, s in enumerate(self.stores):
            r = self.r("roster", i)
            ro = {"STORE_MANAGER": [], "STOCK_CLERK": [], "CASHIER": []}
            for _ in range(2):
                e = emp(person(), "HUMAN", i)
                assign(e, "STORE_MANAGER", i)
                ro["STORE_MANAGER"].append((e, 0, None))
            for k in range(4):
                if k == 0 and r.random() < 0.4:
                    leave = r.randint(25, self.days - 25) * DAY
                    e = emp(person(), "HUMAN", i, term=self.clock.date(leave))
                    assign(e, "STOCK_CLERK", i, 0, leave)
                    ro["STOCK_CLERK"].append((e, 0, leave))
                    self.leavers[i].append((e, leave))
                    e2 = emp(person(), "HUMAN", i, hired=self.clock.date(leave + 3 * DAY))
                    assign(e2, "STOCK_CLERK", i, leave + 3 * DAY)
                    ro["STOCK_CLERK"].append((e2, leave + 3 * DAY, None))
                else:
                    e = emp(person(), "HUMAN", i)
                    assign(e, "STOCK_CLERK", i)
                    ro["STOCK_CLERK"].append((e, 0, None))
            if s["type"] == "STORE":
                for _ in range(4):
                    e = emp(person(), "HUMAN", i)
                    assign(e, "CASHIER", i)
                    ro["CASHIER"].append((e, 0, None))
            self.roster[i] = ro

    def staff(self, store: int, role: str, t: int, salt, exclude: str | None = None) -> str:
        cands = [e for e, f, to in self.roster[store][role] if f <= t and (to is None or t < to) and e != exclude]
        return cands[hint(self.seed, "staff", store, role, salt) % len(cands)]

    def _build_promotions(self) -> None:
        rng = self.r("promos")
        self.promotions, self.promo_scope = [], []
        n_per_region = max(2, self.days // 12)
        for reg in self.regions:
            reg_stores = [i for i in self.store_idx if self.stores[i]["region"] == reg]
            carried = sorted({p.sku for p in self.pairs if p.store in reg_stores})
            for _ in range(n_per_region):
                pid = f"PRM-2026-{len(self.promotions) + 1:06d}"
                start = rng.randint(3, self.days - 16)
                dur = rng.randint(5, 14)
                uplift = rng.uniform(1.6, 2.4)
                self.promotions.append({"promotion_id": pid, "promo_name": f"{reg} {rng.choice(ADJ)} event",
                                        "promo_type": rng.choice(C.PROMO_TYPES), "discount_pct": rng.choice([10, 15, 20, 25, 30, 40]),
                                        "start_date": self.clock.day_str[start], "end_date": self.clock.day_str[start + dur - 1]})
                for sku in sorted(rng.sample(carried, min(len(carried), rng.randint(15, 45)))):
                    for s in reg_stores:
                        pi = self.pair_of.get((s, sku))
                        if pi is not None:
                            self.promo_scope.append({"promotion_id": pid, "store_id": self.stores[s]["store_id"],
                                                     "sku_id": self.skus[sku]["sku_id"]})
                            self.pairs[pi].promos.append((start, start + dur, uplift))

    # ------------------------------------------------------------ planning
    def plan(self) -> None:
        cfg = self.cfg
        store_pairs = [p for p in self.pairs if self.stores[p.store]["type"] == "STORE"]
        weights = [self.skus[p.sku]["pop"] * self.stores[p.store]["size"] for p in store_pairs]
        total_w = sum(weights)
        # ~80% of movement inputs are sale lines; the rest follows from simulation; the tail top-up fixes the exact total.
        sale_budget = cfg["movements"] * cfg["sale_fraction"]
        for p, w in zip(store_pairs, weights):
            p.rate = sale_budget * w / total_w
        # base day weights: weekday seasonality (2026-01-01 is a Thursday)
        wd = [1.25, 1.35, 1.1, 0.85, 0.85, 0.9, 0.95]  # Thu Fri Sat Sun Mon Tue Wed
        self.day_w = [wd[d % 7] for d in range(self.days)]
        # count schedule
        n_invalid = self.n_scn("invalid_count", cfg["counts"])
        n_sched = cfg["counts"] - n_invalid
        rng = self.r("counts")
        risk_f = {"HIGH": 3.0, "MEDIUM": 1.6, "LOW": 1.0}
        wh_pairs = [p for p in self.pairs if self.stores[p.store]["type"] == "WAREHOUSE"]
        n_wh = int(n_sched * 0.05) if wh_pairs else 0
        cands = [(p, (p.rate + 0.5) ** 0.6 * risk_f[self.skus[p.sku]["risk"]]) for p in store_pairs]
        cw = [c[1] for c in cands]
        made, attempts = 0, 0
        self.count_plan = []
        while made < n_sched - n_wh and attempts < n_sched * 20:
            attempts += 1
            p = rng.choices(cands, cw)[0][0]
            if len(p.counts) >= 6:
                continue
            lo_d = (p.active_from // DAY) + 7
            hi_d = (p.active_to // DAY - 2) if p.active_to else self.days - 2
            if hi_d <= lo_d:
                continue
            d = rng.randint(lo_d, hi_d)
            if any(abs(d - c["day"]) < 10 for c in p.counts):
                continue
            p.counts.append({"day": d, "t": at(d, 6, rng.randint(5, 49), rng.randint(0, 59))})
            made += 1
        made_wh, attempts = 0, 0
        while made_wh < n_wh and attempts < n_wh * 50:
            attempts += 1
            p = rng.choice(wh_pairs)
            d = rng.randint(10, self.days - 2)
            if all(abs(d - c["day"]) >= 10 for c in p.counts):
                p.counts.append({"day": d, "t": at(d, 6, rng.randint(5, 49), rng.randint(0, 59))})
                made_wh += 1
        # any shortfall (collisions) goes to store pairs so the count-input total stays exact
        while made + made_wh < n_sched and attempts < n_sched * 100:
            attempts += 1
            p = rng.choices(cands, cw)[0][0]
            lo_d = (p.active_from // DAY) + 7
            hi_d = (p.active_to // DAY - 2) if p.active_to else self.days - 2
            if hi_d <= lo_d:
                continue
            d = rng.randint(lo_d, hi_d)
            if all(abs(d - c["day"]) >= 10 for c in p.counts):
                p.counts.append({"day": d, "t": at(d, 6, rng.randint(5, 49), rng.randint(0, 59))})
                made += 1
        for p in self.pairs:
            p.counts.sort(key=lambda c: c["t"])
            for c in p.counts:
                c.update(scn="BASELINE", theft=0, miscount=0, timing=None, late=None)
        self._plant_count_scenarios()
        self.n_invalid_counts = n_invalid

    def _plant_count_scenarios(self) -> None:
        cfg, rng = self.cfg, self.r("plant-counts")
        all_counts = [(p, i) for p in self.pairs if self.stores[p.store]["type"] == "STORE"
                      for i in range(len(p.counts))]
        rng.shuffle(all_counts)
        used = set()

        def take(n, pred):
            out = []
            for p, i in all_counts:
                if len(out) >= n:
                    break
                if (p.idx, i) in used or not pred(p, i):
                    continue
                used.add((p.idx, i))
                out.append((p, i))
            return out

        def window_ok(p, i, min_days=3):
            prev = p.counts[i - 1]["day"] if i > 0 else p.active_from // DAY
            return p.counts[i]["day"] - prev >= min_days

        nb = self.n_scn("boundary_each", cfg["counts"])
        for k, lab in ((4, "BOUNDARY_BELOW_THRESHOLD"), (5, "BOUNDARY_AT_THRESHOLD"), (6, "BOUNDARY_ABOVE_THRESHOLD")):
            for p, i in take(nb, lambda p, i: window_ok(p, i) and p.rate > 3):
                p.counts[i].update(scn=lab, theft=k)
        for p, i in take(self.n_scn("theft", cfg["counts"]), lambda p, i: window_ok(p, i) and p.rate > 2):
            k = min(40, 6 + int(rng.expovariate(1 / 6)))
            p.counts[i].update(scn="UNEXPLAINED_LOSS", theft=k)
        for p, i in take(self.n_scn("timing", cfg["counts"]), lambda p, i: p.rate > 4):
            p.counts[i].update(scn="COUNT_TIMING_CONFLICT", timing=rng.choice(["CUTOFF_LATE", "CUTOFF_LATE", "SUBMIT_EARLY", "CUTOFF_EARLY"]))
        for p, i in take(self.n_scn("late", cfg["counts"]), lambda p, i: window_ok(p, i, 2) and p.rate > 3):
            p.counts[i].update(scn="LATE_MOVEMENT_BENIGN", late=rng.randint(3, 7))
        # benign look-alike: the counter missed a second location (backroom/top stock). No loss happened.
        for p, i in take(self.n_scn("missed_location", cfg["counts"]), lambda p, i: p.rate > 3):
            p.counts[i].update(scn="MISCOUNT_MISSED_LOCATION", miscount=-rng.randint(5, 12))
        for p in self.pairs:
            for c in p.counts:
                if c["scn"] != "BASELINE":
                    continue
                u = rng.random()
                c["miscount"] = 0 if u < 0.88 else (rng.choice([-1, 1]) if u < 0.97 else
                                                    (rng.choice([-2, 2]) if u < 0.995 else rng.choice([-4, -3, 3, 4])))

    # ---------------------------------------------------------- simulation
    def _day_cum(self, p: Pair, lo: int, hi: int) -> list[float]:
        cum, s = [], 0.0
        for d in range(self.days):
            w = self.day_w[d] if lo <= d < hi else 0.0
            for a, b, u in p.promos:
                if a <= d < b:
                    w *= u
            s += w
            cum.append(s)
        return cum

    def simulate_store(self, p: Pair) -> None:
        r = self.r("sim", p.idx)
        sku = self.skus[p.sku]
        lo = p.active_from // DAY
        hi = (p.active_to // DAY) if p.active_to else self.days
        active_days = max(1, hi - lo)
        weekly = max(0.2, p.rate / active_days * 7 * 1.45)
        s_pt = int(math.ceil(weekly * 1.1)) + 2
        S_pt = int(math.ceil(weekly * 2.6)) + 5
        p.opening = 0 if p.active_from > 0 else r.randint(int(S_pt * 0.6), S_pt)
        heap: list = []
        seq = 0

        def push(t, kind, data=None):
            nonlocal seq
            seq += 1
            heapq.heappush(heap, (t, seq, kind, data))

        hours = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
        hw = [3, 4, 5, 6, 6, 5, 5, 6, 7, 8, 8, 6, 4]
        n_sales = poisson(r, p.rate)
        if n_sales:
            cum = self._day_cum(p, lo, hi)
            days = r.choices(range(self.days), cum_weights=cum, k=n_sales)
            hrs = r.choices(hours, hw, k=n_sales)
            for d, h in zip(days, hrs):
                q = 1 if r.random() < 0.74 else (2 if r.random() < 0.7 else r.randint(3, 6))
                push(at(d, h, r.randint(0, 59), r.randint(0, 59)), "S", {"q": q})
        # benign write-offs and unrecorded shrink
        n_dmg = poisson(r, n_sales * (0.016 if sku["cat"] in PERISHABLE else 0.009))
        for _ in range(n_dmg):
            d = r.randint(lo, hi - 1)
            reason = "EXPIRED" if (sku["cat"] in PERISHABLE and r.random() < 0.5) else (
                "SAMPLING" if (sku["cat"] == "COSMETICS" and r.random() < 0.3) else "DAMAGE")
            push(at(d, r.randint(8, 19), r.randint(0, 59)), "D", {"q": 1 if r.random() < 0.8 else 2, "reason": reason})
        for _ in range(poisson(r, n_sales * 0.0025)):
            d = r.randint(lo, hi - 1)
            push(at(d, r.randint(8, 20), r.randint(0, 59)), "N")
        # counts and planted scenarios around them
        for ci, c in enumerate(p.counts):
            push(c["t"], "C", ci)
            prev_d = p.counts[ci - 1]["day"] if ci else lo
            if c["theft"]:
                d = r.randint(prev_d + 1, c["day"] - 1) if c["day"] - prev_d >= 2 else c["day"] - 1
                push(at(d, r.randint(9, 19), r.randint(0, 59)), "T", {"q": c["theft"], "ci": ci})
            if c["timing"] == "CUTOFF_LATE":
                push(c["t"] + r.randint(2, 5) * 3600, "S", {"q": 1, "forced": ci})
            elif c["timing"] == "CUTOFF_EARLY":
                push(at(c["day"] - 1, 16, r.randint(0, 59)), "S", {"q": 1, "forced": ci})
            if c["late"]:
                push(at(c["day"] - 1, r.randint(15, 19), r.randint(0, 59)), "S", {"q": c["late"], "late": ci})
        for t_adj, q_adj, win, actor, approver in getattr(p, "_planted_adj", []):
            push(t_adj, "A", {"q": q_adj, "reason": "DAMAGE", "win": win, "actor": actor, "approver": approver})
        physical = p.opening
        on_order = 0
        end = (p.active_to if p.active_to else self.clock.end)
        order_stop = end - 14 * DAY if p.active_to else end
        ret_rate = 0.03 if sku["cat"] not in ("FRESH",) else 0.005

        def emit(t, mt, q, sq, x=None):
            ev = Ev(t, mt, q, sq, p.idx, x)
            p.events.append(ev)
            return ev

        def reorder(t):
            nonlocal on_order
            if t >= order_stop or physical + on_order > s_pt:
                return
            qty = S_pt - physical - on_order
            if qty <= 0:
                return
            on_order += qty
            d = t // DAY
            if p.repl == "DC":
                disp_day = d + 1
                existing = self._open_lines.get((p.wh, p.store, p.sku, disp_day))
                if existing is not None:  # second order the same day joins the same transfer line
                    existing["qty"] += qty
                    if existing["arrived"] or existing["scn"] != "UNRESOLVED":
                        existing["arrived"] += qty
                    return
                lead = 1 + hint(self.seed, "lead", p.wh, p.store, disp_day) % 3
                arr = at(disp_day + lead, 8 + hint(self.seed, "arrh", p.wh, p.store, disp_day) % 3,
                         hint(self.seed, "arrm", p.store, p.sku, disp_day) % 60)
                disp = at(disp_day, 15 + hint(self.seed, "disph", p.wh, p.store) % 3, hint(self.seed, "dm", p.store, disp_day) % 60)
                line = {"wh": p.wh, "store": p.store, "sku": p.sku, "disp_day": disp_day, "disp": disp,
                        "arr": arr, "qty": qty, "arrived": qty, "scn": None, "pair": p.idx}
                self._transfer_scenario(line)
                self._open_lines[(p.wh, p.store, p.sku, disp_day)] = line
                arr = line["arr"]
                self.transfer_lines.append(line)
                push(arr, "RT", line)
            else:
                lead = r.randint(2, 5)
                push(at(d + lead, r.randint(7, 10), r.randint(0, 59)), "RV", {"q": qty})

        def ensure(t, need):
            """Emergency replenishment so a planned outbound event can physically happen."""
            nonlocal physical
            if physical >= need:
                return
            add = need - physical + S_pt
            tt = max(t - 1800, at(t // DAY, 7, 5))
            emit(tt, "RECEIPT", add, add, {"emergency": True})
            physical += add

        if p.opening == 0:
            reorder(p.active_from)
        while heap:
            t, _, kind, data = heapq.heappop(heap)
            if t >= end:
                continue
            if kind == "S":
                q = min(data["q"], physical)
                if q <= 0:
                    continue
                physical -= q
                ev = emit(t, "SALE", q, -q, {k: v for k, v in data.items() if k != "q"} or None)
                if "late" not in data and "forced" not in data and r.random() < ret_rate:
                    rd = t + r.randint(1, 14) * DAY
                    rt = at(rd // DAY, r.randint(9, 19), r.randint(0, 59))
                    disp = r.choices(C.RETURN_DISPOSITIONS, [70, 15, 10, 5])[0]
                    push(rt, "R", {"q": q if r.random() < 0.7 else 1, "sale": ev if r.random() < 0.75 else None,
                                   "disp": disp, "reason": r.choice(C.RETURN_REASONS)})
                reorder(t)
            elif kind == "R":
                q = data["q"]
                sq = q if data["disp"] == "RESTOCK" else 0
                physical += sq
                emit(t, "RETURN", q, sq, data)
            elif kind == "RV":
                on_order -= data["q"]
                physical += data["q"]
                emit(t, "RECEIPT", data["q"], data["q"])
            elif kind == "RT":
                line = data
                on_order -= line["qty"]
                if line["arrived"] > 0:
                    physical += line["arrived"]
                    emit(t, "TRANSFER_IN", line["arrived"], line["arrived"], {"line": line})
                if line["scn"] in ("ONCE_RECEIVER", "DOUBLE"):
                    push(t + DAY + 2 * 3600, "W", line)
            elif kind == "W":
                short = data["qty"] - data["arrived"]
                emit(at(t // DAY, 10, r.randint(0, 59)), "ADJUSTMENT", -short, 0,
                     {"reason": "TRANSFER_SHORTAGE", "line": data})
            elif kind == "D":
                q = min(data["q"], physical)
                if q > 0:
                    physical -= q
                    emit(t, "ADJUSTMENT", -q, -q, {"reason": data["reason"]})
                    reorder(t)
            elif kind == "A":
                ensure(t, data["q"])
                physical -= data["q"]
                emit(t, "ADJUSTMENT", -data["q"], -data["q"], {"reason": data["reason"], "planted": data["win"],
                                                                "actor": data["actor"], "approver": data["approver"]})
            elif kind == "N":
                if physical > 0:
                    physical -= 1
                    p.shrink.append(t)
            elif kind == "T":
                ensure(t, data["q"])
                physical -= data["q"]
                p.thefts.append((t, data["q"], data["ci"]))
                reorder(t)
            elif kind == "C":
                c = p.counts[data]
                c["physical"] = physical
                c["counted"] = max(0, physical + c["miscount"])
        p.final_physical = physical

    def _transfer_scenario(self, line: dict) -> None:
        """Plant transfer scenarios on DC replenishment lines (decided when the line is created)."""
        d = line["disp_day"]
        if d > self.days - 16:
            return  # too close to the end: ordinary in-transit only
        if hint(self.seed, "tdelay", line["wh"], line["store"], d) % 1000 < SCENARIO_RATES["transfer_delay"] * 1000:
            line["arr"] = at(d + 3 + 1 + hint(self.seed, "tdd", line["wh"], line["store"], d) % 5, 9,
                             hint(self.seed, "tdm", line["store"], d) % 60)
            line["delayed"] = True
        if line["qty"] < 3:
            return
        u = hint(self.seed, "tscn", line["wh"], line["store"], line["sku"], d) % 100000 / 100000
        r1, r2 = SCENARIO_RATES["transfer_double"], SCENARIO_RATES["transfer_unresolved"]
        r3 = SCENARIO_RATES["transfer_once"]
        short = 1 + hint(self.seed, "tshort", line["store"], line["sku"], d) % min(4, line["qty"] - 1)
        if u < r1:
            line.update(scn="DOUBLE", arrived=line["qty"] - short)
        elif u < r1 + r2:
            never = hint(self.seed, "tnever", line["store"], line["sku"], d) % 4 == 0
            line.update(scn="UNRESOLVED", arrived=0 if never else line["qty"] - short)
        elif u < r1 + r2 + r3:
            side = "ONCE_RECEIVER" if hint(self.seed, "tside", line["store"], line["sku"], d) % 2 else "ONCE_SENDER"
            line.update(scn=side, arrived=line["qty"] - short)

    def plant_adjustment_windows(self) -> None:
        """Planted pattern: one clerk posts 4-6 DAMAGE write-offs within 12 days, approved by one manager."""
        rng = self.r("plant-adj")
        cands = [p for p in self.pairs if self.stores[p.store]["type"] == "STORE" and p.rate > 4 and p.active_to is None]
        rng.shuffle(cands)
        self.adj_windows = []
        for p in cands[: self.n_scn("repeated_adj", self.cfg["counts"])]:
            start = rng.randint(max(5, p.active_from // DAY + 5), self.days - 15)
            n = rng.randint(4, 6)
            # the same clerk must hold the role for the whole window (leavers are excluded)
            clerks = [e for e, f, to in self.roster[p.store]["STOCK_CLERK"]
                      if f <= at(start, 0) and (to is None or to > at(start + 12, 23))]
            if not clerks:
                continue
            actor = clerks[hint(self.seed, "adj-actor", p.idx) % len(clerks)]
            approver = self.staff(p.store, "STORE_MANAGER", at(start, 12), ("adj-appr", p.idx), exclude=actor)
            days = sorted(rng.sample(range(start, start + 12), n))
            win = f"ADJWIN-{len(self.adj_windows) + 1:05d}"
            self.adj_windows.append({"win": win, "pair": p.idx, "start": at(days[0], 0), "end": at(days[-1], 23),
                                     "n": n, "actor": actor, "approver": approver})
            p._planted_adj = [(at(d, rng.randint(12, 19), rng.randint(0, 59)), rng.randint(2, 4), win, actor, approver)
                              for d in days]

    def simulate(self) -> None:
        self.transfer_lines: list[dict] = []
        self._open_lines: dict = {}
        self.plant_adjustment_windows()
        for p in self.pairs:
            if self.stores[p.store]["type"] == "STORE":
                self.simulate_store(p)
        self.lines_by_wh_sku = defaultdict(list)
        for l in self.transfer_lines:
            self.lines_by_wh_sku[(l["wh"], l["sku"])].append(l)
        for p in self.pairs:
            if self.stores[p.store]["type"] == "WAREHOUSE":
                self.simulate_warehouse(p)
        for p in self.pairs:
            p.events.sort(key=lambda e: (e.t, e.mt))

    def simulate_warehouse(self, p: Pair) -> None:
        r = self.r("simwh", p.idx)
        lines = sorted(self.lines_by_wh_sku.get((p.store, p.sku), []), key=lambda l: (l["disp"], l["store"]))
        weekly = max(1.0, sum(l["qty"] for l in lines) / max(1, self.days) * 7)
        s_pt, S_pt = int(weekly * 1.5) + 5, int(weekly * 3.5) + 10
        p.opening = S_pt
        heap, seq = [], 0

        def push(t, kind, data=None):
            nonlocal seq
            seq += 1
            heapq.heappush(heap, (t, seq, kind, data))

        for l in lines:
            push(l["disp"], "X", l)
            if l["scn"] in ("ONCE_SENDER", "DOUBLE") and l["arr"] + DAY < self.clock.end:
                push(at(l["arr"] // DAY + 2, 10, r.randint(0, 59)), "W", l)
        for ci, c in enumerate(p.counts):
            push(c["t"], "C", ci)
        for _ in range(poisson(r, len(lines) * 0.01)):
            push(at(r.randint(1, self.days - 1), r.randint(8, 12), r.randint(0, 59)), "D", {"q": 1, "reason": "DAMAGE"})
        physical, on_order = p.opening, 0

        def emit(t, mt, q, sq, x=None):
            ev = Ev(t, mt, q, sq, p.idx, x)
            p.events.append(ev)
            return ev

        while heap:
            t, _, kind, data = heapq.heappop(heap)
            if t >= self.clock.end:
                continue
            if kind == "X":
                if physical < data["qty"]:
                    add = data["qty"] - physical + S_pt
                    emit(at(t // DAY, 6, 30 + r.randint(0, 20)), "RECEIPT", add, add, {"emergency": True})
                    physical += add
                physical -= data["qty"]
                emit(t, "TRANSFER_OUT", data["qty"], -data["qty"], {"line": data})
                if physical + on_order <= s_pt:
                    q = S_pt - physical - on_order
                    on_order += q
                    push(at(t // DAY + 3, r.randint(6, 11), r.randint(0, 59)), "RV", {"q": q})
            elif kind == "RV":
                on_order -= data["q"]
                physical += data["q"]
                emit(t, "RECEIPT", data["q"], data["q"])
            elif kind == "W":
                short = data["qty"] - data["arrived"]
                emit(t, "ADJUSTMENT", -short, 0, {"reason": "TRANSFER_SHORTAGE", "line": data})
            elif kind == "D":
                if physical > 0:
                    physical -= 1
                    emit(t, "ADJUSTMENT", -1, -1, {"reason": "DAMAGE"})
            elif kind == "C":
                c = p.counts[data]
                c["physical"] = physical
                c["counted"] = max(0, physical + c["miscount"])
        p.final_physical = physical

    # --------------------------------------------------- reporting anomalies
    def inject_anomalies(self) -> None:
        cfg, rng = self.cfg, self.r("anomalies")
        stores_by_region = defaultdict(list)
        for i in self.store_idx:
            stores_by_region[self.stores[i]["region"]].append(i)
        referenced = set()
        for p in self.pairs:
            for e in p.events:
                if e.mt == "RETURN" and e.x and e.x.get("sale") is not None:
                    referenced.add(id(e.x["sale"]))
        evs = [e for p in self.pairs for e in p.events]
        order = list(range(len(evs)))
        rng.shuffle(order)
        taken: set[int] = set()

        def clean(e):
            return (id(e) not in taken and id(e) not in referenced and
                    not (e.x and (e.x.get("late") is not None or e.x.get("forced") is not None or e.x.get("planted"))))

        def pick(n, pred):
            out = []
            for k in order:
                if len(out) >= n:
                    break
                e = evs[k]
                if clean(e) and pred(e):
                    taken.add(id(e))
                    out.append(e)
            return out

        def is_store(e):
            return self.stores[self.pairs[e.p].store]["type"] == "STORE"

        M = cfg["movements"]
        self.anom: dict[str, list[Ev]] = {}
        for e in pick(self.n_scn("wrong_store", M), lambda e: e.mt in ("SALE", "RECEIPT") and is_store(e)):
            st = self.pairs[e.p].store
            others = [s for s in stores_by_region[self.stores[st]["region"]] if s != st] or [s for s in self.store_idx if s != st]
            wrong = others[rng.randrange(len(others))]
            corrected = rng.random() < 0.5
            e.x = dict(e.x or {}, anom="WRONG_STORE", wrong=wrong, corrected=corrected, corr_delay=rng.randint(1, 4) * DAY)
            e.post = corrected
            self.anom.setdefault("WRONG_STORE", []).append(e)
        for e in pick(self.n_scn("missing_reason", M),
                      lambda e: e.mt == "ADJUSTMENT" and e.x and e.x.get("reason") in ("DAMAGE", "EXPIRED", "SAMPLING")):
            corrected = rng.random() < 0.6
            e.x = dict(e.x, anom="MISSING_REASON", corrected=corrected, corr_delay=rng.randint(1, 3) * DAY)
            e.post = corrected
            self.anom.setdefault("MISSING_REASON", []).append(e)
        leavers = dict(self.leavers)
        for e in pick(self.n_scn("role_expired", M),
                      lambda e: e.mt == "RECEIPT" and self.pairs[e.p].store in leavers and
                      any(e.t > lv + DAY for _, lv in leavers[self.pairs[e.p].store])):
            st = self.pairs[e.p].store
            emp = [x for x, lv in leavers[st] if e.t > lv + DAY][0]
            e.x = dict(e.x or {}, anom="ROLE_EXPIRED", bad_actor=emp, corrected=True, corr_delay=DAY)
            self.anom.setdefault("ROLE_EXPIRED", []).append(e)
        for e in pick(self.n_scn("duplicate", M), lambda e: e.mt in ("SALE", "RECEIPT", "TRANSFER_IN", "TRANSFER_OUT", "RETURN")):
            e.x = dict(e.x or {}, anom="DUPLICATE", dup_delay=rng.randint(300, 2 * DAY))
            self.anom.setdefault("DUPLICATE", []).append(e)
        for e in pick(self.n_scn("conflict", M), lambda e: e.mt == "SALE"):
            e.x = dict(e.x or {}, anom="CONFLICT", dup_delay=rng.randint(DAY, 3 * DAY))
            self.anom.setdefault("CONFLICT", []).append(e)
        for e in pick(self.n_scn("rering", M), lambda e: e.mt == "SALE" and is_store(e)):
            e.x = dict(e.x or {}, anom="RERING", wrong_q=e.q + rng.randint(1, 3), fix_delay=rng.randint(600, 5400))
            self.anom.setdefault("RERING", []).append(e)

    # ----------------------------------------- derived ledger, corrections, labels
    def derive_counts(self) -> None:
        """Recompute the ledger the backend must end up with; post store write-offs; derive count labels."""
        rng = self.r("derive")
        self.corrections = 0
        for p in self.pairs:
            if not p.counts:
                continue
            evs = p.events
            cleared_at = -1
            for ci, c in enumerate(p.counts):
                t = c["t"]
                sub = t + rng.randint(300, 1500)
                cutoff = t
                if c["timing"] == "CUTOFF_LATE":
                    cutoff = t + rng.randint(8, 12) * 3600
                elif c["timing"] == "SUBMIT_EARLY":
                    sub = t - rng.randint(300, 2400)
                elif c["timing"] == "CUTOFF_EARLY":
                    cutoff = at(c["day"] - 1, 12, rng.randint(0, 59))
                c.update(counted_at=t, cutoff=cutoff, submitted=sub)
                if c["late"] is not None:
                    for e in evs:
                        if e.x and e.x.get("late") == ci:
                            e.rt = sub + rng.randint(1, 3) * DAY + rng.randint(0, 3600)
                            c["late_qty"] = e.q
                    if "late_qty" not in c:  # shelf was empty: the late sale never happened
                        c["late"], c["scn"] = None, "BASELINE"
                ledger, explained = p.opening, 0
                for e in evs:
                    if e.t > cutoff:
                        break
                    if e.post:
                        ledger += e.sq
                    elif e.x and e.x.get("anom") in ("WRONG_STORE", "MISSING_REASON"):
                        explained += -e.sq
                thefts = [q for (tt, q, _) in p.thefts if cleared_at < tt <= cutoff]
                shrinks = [tt for tt in p.shrink if cleared_at < tt <= cutoff]
                variance = c["counted"] - ledger
                c.update(oracle_expected=ledger, theft_out=sum(thefts), shrink_out=len(shrinks), explained=explained,
                         variance=variance)
                lo, hi = min(t, cutoff), max(t, cutoff)
                between = any(e.post and e.sq != 0 and lo < e.t <= hi for e in evs)
                c["timing_effective"] = c["timing"] in ("CUTOFF_LATE", "SUBMIT_EARLY") or (
                    c["timing"] == "CUTOFF_EARLY" and between)
                if c["timing_effective"]:
                    c["expected_decision"], c["expected_code"] = "TIMING_REVIEW", "COUNT_TIMING_REVIEW"
                elif c["theft_out"] >= THRESHOLD:
                    c["expected_decision"], c["expected_code"] = "SHRINKAGE_ALERT", "UNEXPLAINED_SHORTAGE"
                else:
                    c["expected_decision"], c["expected_code"] = "NO_ALERT", None
                if c["scn"] == "BASELINE":
                    if c["theft_out"] > 0:
                        c["scn"] = "UNEXPLAINED_LOSS_CARRYOVER"
                    elif explained > 0:
                        c["scn"] = "QUARANTINE_EXPLAINED"
                    elif c["shrink_out"] or c["miscount"]:
                        c["scn"] = "BENIGN_NOISE"
                if c["scn"] == "COUNT_TIMING_CONFLICT" and not c["timing_effective"]:
                    c["scn"] = "COUNT_CUTOFF_EARLY_NO_ACTIVITY"
                # The store writes off the variance not explained by quarantined rows (85% of the time).
                amount = variance + explained
                end = p.active_to or self.clock.end
                ct = at(c["day"] + 1, 10, rng.randint(0, 59), rng.randint(0, 59))
                if not c["timing_effective"] and amount != 0 and ct < end and rng.random() < 0.85:
                    ce = Ev(ct, "ADJUSTMENT", amount, amount, p.idx, {"reason": "COUNT_CORRECTION", "count": ci})
                    evs.append(ce)
                    evs.sort(key=lambda e: (e.t, e.mt))
                    cleared_at = cutoff
                    c["corrected"] = amount
                    self.corrections += 1
        # invalid counts (quarantine): a cashier or a leaver submits a count
        self.invalid_counts = []
        stores_pairs = defaultdict(list)
        for p in self.pairs:
            if self.stores[p.store]["type"] == "STORE":
                stores_pairs[p.store].append(p)
        r2 = self.r("invalid-counts")
        st_keys = sorted(stores_pairs)
        for n in range(self.n_invalid_counts):
            s = st_keys[r2.randrange(len(st_keys))]
            p = stores_pairs[s][r2.randrange(len(stores_pairs[s]))]
            hi = (p.active_to // DAY - 2) if p.active_to else self.days - 2
            d = r2.randint(min(hi, max(8, p.active_from // DAY + 8)), hi)
            t = at(d, 6, r2.randint(5, 49))
            lv = [e for e, when in self.leavers.get(s, []) if t > when + DAY]
            actor = lv[0] if lv and r2.random() < 0.5 else self.staff(s, "CASHIER", t, ("inv", n))
            self.invalid_counts.append({"pair": p.idx, "t": t, "actor": actor, "qty": r2.randint(0, 30)})

    def rows_per_event(self, e: Ev) -> int:
        a = (e.x or {}).get("anom")
        if a in ("WRONG_STORE", "MISSING_REASON"):
            return 2 if e.x["corrected"] else 1
        if a in ("ROLE_EXPIRED", "DUPLICATE", "CONFLICT"):
            return 2
        if a == "RERING":
            return 3
        return 1

    def top_up(self) -> None:
        """Hit the movement-input target exactly with end-of-period sales after each pair's last count."""
        total = sum(self.rows_per_event(e) for p in self.pairs for e in p.events)
        delta = self.cfg["movements"] - total
        self.stats["topup_delta"] = delta
        rng = self.r("topup")
        end = self.clock.end
        last_needed = {}
        for p in self.pairs:
            lc = max([c["cutoff"] for c in p.counts] + [c["counted_at"] for c in p.counts] + [0])
            last_needed[p.idx] = lc + 2 * DAY
        for ic in getattr(self, "invalid_counts", []):
            last_needed[ic["pair"]] = max(last_needed[ic["pair"]], ic["t"] + DAY)
        if delta > 0:
            cands = []
            for p in self.pairs:
                if self.stores[p.store]["type"] != "STORE" or p.active_to is not None:
                    continue
                lo = max(last_needed[p.idx], (p.events[-1].t if p.events else 0) + 60, end - 10 * DAY)
                d_lo = lo // DAY + 1
                if d_lo < self.days:
                    cands.append((p, d_lo))
            avail = [max(0, p.final_physical) for p, _ in cands]
            restocked = set()
            added = 0
            while added < delta and cands:
                if sum(avail) == 0:
                    # every candidate shelf is empty: a tail replenishment receipt (also a real movement input)
                    i = rng.randrange(len(cands))
                    p, d_lo = cands[i]
                    if i in restocked:
                        continue
                    q = rng.randint(10, 20)
                    p.events.append(Ev(at(d_lo, 7, rng.randint(5, 50)), "RECEIPT", q, q, p.idx, {"tail": True}))
                    restocked.add(i)
                    avail[i] += q
                    p.final_physical += q
                    added += 1
                    continue
                i = rng.choices(range(len(cands)), avail)[0]
                p, d_lo = cands[i]
                d = rng.randint(d_lo, self.days - 1)
                t = at(d, rng.randint(8, 20), rng.randint(0, 59), rng.randint(0, 59))
                p.events.append(Ev(t, "SALE", 1, -1, p.idx, {"tail": True}))
                p.final_physical -= 1
                avail[i] -= 1
                added += 1
            for p, _ in cands:
                p.events.sort(key=lambda e: (e.t, e.mt))
            self.stats["topup_added"] = added
        elif delta < 0:
            referenced = {id(e.x["sale"]) for p in self.pairs for e in p.events
                          if e.mt == "RETURN" and e.x and e.x.get("sale") is not None}
            removable = []
            for p in self.pairs:
                for e in p.events:
                    if (e.mt == "SALE" and e.t > last_needed[p.idx] and id(e) not in referenced and
                            not (e.x and (e.x.get("anom") or e.x.get("late") is not None or e.x.get("forced") is not None))):
                        removable.append(e)
            removable.sort(key=lambda e: (e.t, e.p, e.mt, e.q))
            rng.shuffle(removable)  # spread trimming across the eligible tail, no end-of-period cliff
            drop = set(id(e) for e in removable[:-delta])
            for p in self.pairs:
                before = len(p.events)
                p.final_physical += sum(-e.sq for e in p.events if id(e) in drop)
                p.events = [e for e in p.events if id(e) not in drop]
                self.stats["topup_removed"] += before - len(p.events)

    # ------------------------------------------------------------- ids & rows
    def shift_id(self, store: int, t: int) -> str:
        d, sec = divmod(t, DAY)
        hour = sec // 3600
        if self.stores[store]["type"] == "WAREHOUSE":
            kind = 0 if hour < 14 else 1
        else:
            kind = 0 if hour < 13 else 1
        return f"SHF-2026-{(d * len(self.stores) + store) * 2 + kind + 1:07d}"

    def build_outputs(self) -> dict:
        rng = self.r("rows")
        clock = self.clock
        groups = defaultdict(list)
        for l in self.transfer_lines:
            groups[(l["disp_day"], l["wh"], l["store"])].append(l)
        self.transfers = []
        for n, key in enumerate(sorted(groups)):
            tid = f"TRF-2026-{n + 1:06d}"
            d, wh, st = key
            for l in groups[key]:
                l["tid"] = tid
            self.transfers.append({"transfer_id": tid, "from_store_id": self.stores[wh]["store_id"],
                                   "to_store_id": self.stores[st]["store_id"], "created_at": clock.iso(at(d, 12)),
                                   "expected_arrival_at": clock.iso(at(d + 3, 12)), "lines": groups[key], "day": d})
        all_evs = [e for p in self.pairs for e in p.events]
        all_evs.sort(key=lambda e: (e.t, self.pairs[e.p].store, self.pairs[e.p].sku, e.mt, e.q))
        po_keys = sorted({(e.t // DAY, self.pairs[e.p].store) for e in all_evs if e.mt == "RECEIPT"})
        po_id = {k: f"PO-2026-{i + 1:06d}" for i, k in enumerate(po_keys)}
        n_store = len(self.store_idx)

        def pos_id(store, t):
            return f"POS-2026-{(t // DAY) * n_store + store + 1:07d}"

        sys_of = {"SALE": "POS", "RETURN": "POS", "RECEIPT": "WMS", "TRANSFER_IN": "WMS", "TRANSFER_OUT": "WMS",
                  "ADJUSTMENT": "STORE_OPS", "REVERSAL": "POS"}
        prefix = {"POS": "POS", "WMS": "WMS", "STORE_OPS": "OPS"}
        counters: dict[str, int] = defaultdict(int)
        state = {"mov": 0, "ret": 0}

        def next_mid():
            state["mov"] += 1
            return f"MOV-2026-{state['mov']:08d}"

        def next_sid(sys):
            counters[sys] += 1
            return f"{prefix[sys]}-{counters[sys]:09d}"

        # count ids (scheduled + invalid) in time order
        count_list = [(c["counted_at"], p.store, p.sku, "C", p.idx, ci) for p in self.pairs for ci, c in enumerate(p.counts)]
        count_list += [(ic["t"], self.pairs[ic["pair"]].store, self.pairs[ic["pair"]].sku, "I", ic["pair"], n)
                       for n, ic in enumerate(self.invalid_counts)]
        count_list.sort()
        count_id = {(kind, pi, ci): f"CNT-2026-{n + 1:06d}" for n, (_, _, _, kind, pi, ci) in enumerate(count_list)}

        rows: list[dict] = []
        gt_mov: list[dict] = []
        for e in all_evs:
            p = self.pairs[e.p]
            st, sk = self.stores[p.store], self.skus[p.sku]
            x = e.x or {}
            sys = sys_of[e.mt]
            e.mid, e.sid = next_mid(), next_sid(sys)
            if e.rt is None:
                lo, hi = {"POS": (5, 180), "WMS": (30, 900), "STORE_OPS": (60, 1800)}[sys]
                delay = rng.randint(lo, hi)
                if sys == "POS" and rng.random() < 0.015:
                    delay = rng.randint(3600, 6 * 3600)
                e.rt = e.t + delay
            row = dict.fromkeys(MOVEMENT_FIELDS)
            row.update(source_system=sys, source_event_id=e.sid, movement_id=e.mid, movement_type=e.mt,
                       store_id=st["store_id"], sku_id=sk["sku_id"], quantity=e.q, occurred_at=clock.iso(e.t),
                       received_at=clock.iso(e.rt), shift_id=self.shift_id(p.store, e.t))
            salt = (e.p, e.t, e.mt)
            if e.mt == "SALE":
                row.update(actor_id=self.svc_pos, source_doc_id=pos_id(p.store, e.t))
            elif e.mt == "RETURN":
                state["ret"] += 1
                sale = x.get("sale")
                row.update(actor_id=self.staff(p.store, "CASHIER", e.t, salt), source_doc_id=pos_id(p.store, e.t),
                           return_id=f"RET-2026-{state['ret']:06d}", disposition=x["disp"], return_reason=x["reason"],
                           original_sale_movement_id=(sale.mid if sale is not None and sale.post and sale.mid else None))
            elif e.mt == "RECEIPT":
                actor = self.svc_wms if st["type"] == "WAREHOUSE" else self.staff(p.store, "STOCK_CLERK", e.t, salt)
                row.update(actor_id=actor, source_doc_id=po_id[(e.t // DAY, p.store)])
            elif e.mt in ("TRANSFER_IN", "TRANSFER_OUT"):
                actor = self.svc_wms if st["type"] == "WAREHOUSE" else self.staff(p.store, "STOCK_CLERK", e.t, salt)
                row.update(actor_id=actor, transfer_id=x["line"]["tid"])
            elif e.mt == "ADJUSTMENT":
                actor = x.get("actor") or self.staff(p.store, "STOCK_CLERK", e.t, salt)
                row.update(actor_id=actor, reason_code=x["reason"],
                           approved_by=x.get("approver") or self.staff(p.store, "STORE_MANAGER", e.t, salt, exclude=actor))
                if x["reason"] == "TRANSFER_SHORTAGE":
                    row["transfer_id"] = x["line"]["tid"]
                if x["reason"] == "COUNT_CORRECTION":
                    row["count_ref"] = count_id[("C", e.p, x["count"])]
            a = x.get("anom")
            if a is None:
                rows.append(row)
                continue
            gt = {"grain": "MOVEMENT", "entity_key": f"{sys}|{e.sid}", "movement_id": e.mid, "store_id": st["store_id"],
                  "sku_id": sk["sku_id"], "occurred_at": row["occurred_at"]}
            if a == "WRONG_STORE":
                bad = dict(row, store_id=self.stores[x["wrong"]]["store_id"])
                rows.append(bad)
                gt.update(scenario="WRONG_STORE", is_anomaly=1, expected_decision="QUARANTINE",
                          expected_code="WRONG_STORE_REFERENCE", explain_qty=0 if x["corrected"] else -e.sq,
                          notes=f"submitted {bad['store_id']}; {row['source_doc_id']} belongs to {row['store_id']}; "
                                f"{'corrected' if x['corrected'] else 'not corrected'}")
                if x["corrected"]:
                    rows.append(dict(row, received_at=clock.iso(e.rt + x["corr_delay"])))
                    gt["correction_expected"] = "POSTED"
            elif a == "MISSING_REASON":
                rows.append(dict(row, reason_code=None))
                gt.update(scenario="ADJUSTMENT_REASON_MISSING", is_anomaly=1, expected_decision="QUARANTINE",
                          expected_code="ADJUSTMENT_REASON_MISSING", explain_qty=0 if x["corrected"] else -e.sq,
                          notes=("corrected with " + row["reason_code"]) if x["corrected"] else "not corrected")
                if x["corrected"]:
                    rows.append(dict(row, received_at=clock.iso(e.rt + x["corr_delay"])))
                    gt["correction_expected"] = "POSTED"
            elif a == "ROLE_EXPIRED":
                rows.append(dict(row, actor_id=x["bad_actor"]))
                rows.append(dict(row, received_at=clock.iso(e.rt + x["corr_delay"])))
                gt.update(scenario="ROLE_OUTSIDE_EFFECTIVE_DATES", is_anomaly=1, expected_decision="QUARANTINE",
                          expected_code="ACTOR_NOT_AUTHORIZED", correction_expected="POSTED",
                          notes=f"{x['bad_actor']} left before {row['occurred_at']}")
            elif a == "DUPLICATE":
                rows.append(row)
                rows.append(dict(row, received_at=clock.iso(e.rt + x["dup_delay"])))
                gt.update(scenario="FEED_REPLAY", is_anomaly=1, expected_decision="DUPLICATE",
                          notes="exact replay of a posted row")
            elif a == "CONFLICT":
                rows.append(row)
                rows.append(dict(row, quantity=e.q + 1, received_at=clock.iso(e.rt + x["dup_delay"])))
                gt.update(scenario="PAYLOAD_CONFLICT", is_anomaly=1, expected_decision="CONFLICT",
                          expected_code="CONFLICT_PAYLOAD", notes="same event id, quantity changed")
            elif a == "RERING":
                rows.append(dict(row, quantity=x["wrong_q"]))
                rid, rsid = next_mid(), next_sid("POS")
                fix = clock.iso(e.rt + x["fix_delay"])
                rev = dict.fromkeys(MOVEMENT_FIELDS)
                rev.update(source_system="POS", source_event_id=rsid, movement_id=rid, movement_type="REVERSAL",
                           store_id=row["store_id"], sku_id=row["sku_id"], quantity=x["wrong_q"],
                           occurred_at=row["occurred_at"], received_at=fix, actor_id=self.svc_pos,
                           shift_id=row["shift_id"], reverses_movement_id=e.mid, memo="POS void: wrong quantity scanned")
                rows.append(rev)
                nid, nsid = next_mid(), next_sid("POS")
                rows.append(dict(row, source_event_id=nsid, movement_id=nid, received_at=fix))
                gt.update(scenario="POS_RERING_REVERSAL", is_anomaly=0, expected_decision="POSTED",
                          notes=f"posted qty {x['wrong_q']}, reversed by {rid}, replaced by {nid} (qty {e.q})")
            gt_mov.append(gt)
        # counts input + count ground truth
        count_rows, gt_cnt = [], []
        for (_, _, _, kind, pi, ci) in count_list:
            p = self.pairs[pi]
            st, sk = self.stores[p.store], self.skus[p.sku]
            cid = count_id[(kind, pi, ci)]
            if kind == "C":
                c = p.counts[ci]
                count_rows.append({"source_system": "STORE_OPS", "count_id": cid, "store_id": st["store_id"],
                                   "sku_id": sk["sku_id"], "counted_qty": c["counted"],
                                   "counted_at": clock.iso(c["counted_at"]), "count_cutoff_at": clock.iso(c["cutoff"]),
                                   "submitted_at": clock.iso(c["submitted"]),
                                   "counted_by": self.staff(p.store, "STOCK_CLERK", c["counted_at"], ("cnt", pi, ci)),
                                   "shift_id": self.shift_id(p.store, c["counted_at"]),
                                   "count_type": "SCHEDULED" if (hint(self.seed, "ct", pi, ci) % 10) < 8 else "BLIND"})
                gt_cnt.append({"grain": "COUNT", "entity_key": cid, "store_id": st["store_id"], "sku_id": sk["sku_id"],
                               "scenario": c["scn"],
                               "is_anomaly": 1 if c["expected_decision"] in ("SHRINKAGE_ALERT", "TIMING_REVIEW") else 0,
                               "expected_decision": c["expected_decision"], "expected_code": c["expected_code"],
                               "hidden_loss_units": c["theft_out"], "miscount_units": c["miscount"],
                               "unrecorded_shrink_units": c["shrink_out"], "explain_qty": c["explained"],
                               "oracle_expected_qty": c["oracle_expected"], "oracle_counted_qty": c["counted"],
                               "oracle_variance_qty": c["variance"], "timing_subtype": c["timing"],
                               "late_movement_qty": c.get("late_qty"), "correction_posted_qty": c.get("corrected")})
            else:
                ic = self.invalid_counts[ci]
                count_rows.append({"source_system": "STORE_OPS", "count_id": cid, "store_id": st["store_id"],
                                   "sku_id": sk["sku_id"], "counted_qty": ic["qty"], "counted_at": clock.iso(ic["t"]),
                                   "count_cutoff_at": clock.iso(ic["t"]), "submitted_at": clock.iso(ic["t"] + 600),
                                   "counted_by": ic["actor"], "shift_id": self.shift_id(p.store, ic["t"]),
                                   "count_type": "AD_HOC"})
                gt_cnt.append({"grain": "COUNT", "entity_key": cid, "store_id": st["store_id"], "sku_id": sk["sku_id"],
                               "scenario": "COUNT_BY_UNAUTHORIZED_ACTOR", "is_anomaly": 1,
                               "expected_decision": "QUARANTINE", "expected_code": "ACTOR_NOT_AUTHORIZED",
                               "notes": f"{ic['actor']} has no SUBMIT_COUNT authority at the count time"})
        gt_trf = []
        for t in self.transfers:
            if at(t["day"], 15) >= self.clock.end:
                continue
            scns = {l.get("scn") for l in t["lines"]}
            delayed = any(l.get("delayed") for l in t["lines"])
            in_transit = any(l["arr"] >= self.clock.end for l in t["lines"])
            if "DOUBLE" in scns:
                s_, dec, code, an = "TRANSFER_DOUBLE_COUNT", "TRANSFER_ALERT", "TRANSFER_DOUBLE_COUNT", 1
            elif "UNRESOLVED" in scns:
                s_, dec, code, an = "TRANSFER_SHORT_UNRESOLVED", "TRANSFER_ALERT", "TRANSFER_OVERDUE_DISCREPANCY", 1
            elif {"ONCE_RECEIVER", "ONCE_SENDER"} & scns:
                s_, dec, code, an = "TRANSFER_SHORT_WRITTEN_OFF_ONCE", "NO_ALERT", None, 0
            elif delayed:
                s_, dec, code, an = "TRANSFER_DELAYED_WITHIN_GRACE", "NO_ALERT", None, 0
            elif in_transit:
                s_, dec, code, an = "TRANSFER_IN_TRANSIT_AT_END", "NO_ALERT", None, 0
            else:
                s_, dec, code, an = "TRANSFER_NORMAL", "NO_ALERT", None, 0
            gt_trf.append({"grain": "TRANSFER", "entity_key": t["transfer_id"], "store_id": t["to_store_id"],
                           "related_store_id": t["from_store_id"], "scenario": s_, "is_anomaly": an,
                           "expected_decision": dec, "expected_code": code,
                           "notes": ";".join(f"{self.skus[l['sku']]['sku_id']}:{l['qty']}->{l['arrived']}:{l['scn']}"
                                             for l in t["lines"] if l.get("scn"))})
        gt_adj = []
        for w in self.adj_windows:
            p = self.pairs[w["pair"]]
            gt_adj.append({"grain": "ADJUSTMENT_WINDOW", "entity_key": w["win"], "store_id": self.stores[p.store]["store_id"],
                           "sku_id": self.skus[p.sku]["sku_id"], "scenario": "REPEATED_NEGATIVE_ADJUSTMENTS",
                           "is_anomaly": 1, "expected_decision": "PATTERN_ALERT",
                           "expected_code": "REPEATED_NEGATIVE_ADJUSTMENTS", "window_start": clock.iso(w["start"]),
                           "window_end": clock.iso(w["end"]), "notes": f"{w['n']} DAMAGE write-offs by {w['actor']}"})
        rows.sort(key=lambda r: (r["received_at"], r["movement_id"], r["source_event_id"], str(r["store_id"]),
                                 str(r["reason_code"]), str(r["actor_id"]), r["quantity"]))
        count_rows.sort(key=lambda r: (r["submitted_at"], r["count_id"]))
        return {"movements": rows, "counts": count_rows, "po": po_keys, "pos_n": n_store,
                "gt": gt_mov + gt_cnt + gt_trf + gt_adj}

    # ------------------------------------------------------------------ export
    def master_rows(self, out: dict) -> dict[str, list[dict]]:
        clock = self.clock
        m: dict[str, list[dict]] = {}
        m["stores.csv"] = [{"store_id": s["store_id"], "store_name": s["name"], "store_type": s["type"],
                            "region_code": s["region"], "timezone": s["tz"], "opened_on": s["opened"],
                            "status": "ACTIVE", "closed_on": None} for s in self.stores]
        m["skus.csv"] = [{"sku_id": k["sku_id"], "sku_name": f"{k['brand']} {k['name']}", "category": k["cat"],
                          "brand": k["brand"], "uom": "EA", "unit_cost_cents": k["cost"], "unit_price_cents": k["price"],
                          "shrink_risk": k["risk"], "status": k["status"], "introduced_on": k["introduced"]}
                         for k in self.skus]
        m["store_skus.csv"] = sorted(({
            "store_id": self.stores[p.store]["store_id"], "sku_id": self.skus[p.sku]["sku_id"],
            "active_from": clock.iso(p.active_from), "active_to": clock.iso(p.active_to) if p.active_to else None,
            "opening_qty": p.opening, "opening_as_of": clock.iso(p.active_from), "replenishment_source": p.repl,
            "source_warehouse_id": self.stores[p.wh]["store_id"] if p.wh is not None else None} for p in self.pairs),
            key=lambda r: (r["store_id"], r["sku_id"]))
        m["employees.csv"] = self.employees
        m["role_assignments.csv"] = self.assignments
        shifts = []
        for d in range(self.days):
            for i, s in enumerate(self.stores):
                spans = [("DAY", 6, 14), ("SWING", 14, 22)] if s["type"] == "WAREHOUSE" else [("OPEN", 5, 13), ("CLOSE", 13, 21)]
                for k, (name, a, b) in enumerate(spans):
                    shifts.append({"shift_id": f"SHF-2026-{(d * len(self.stores) + i) * 2 + k + 1:07d}",
                                   "store_id": s["store_id"], "shift_date": clock.day_str[d], "shift_type": name,
                                   "start_at": clock.iso(at(d, a)), "end_at": clock.iso(at(d, b))})
        m["shifts.csv"] = shifts
        m["promotions.csv"] = self.promotions
        m["promotion_scopes.csv"] = sorted(self.promo_scope, key=lambda r: (r["promotion_id"], r["store_id"], r["sku_id"]))
        docs = []
        for d in range(self.days):
            for i in self.store_idx:
                docs.append({"doc_id": f"POS-2026-{d * out['pos_n'] + i + 1:07d}", "doc_type": "POS_BATCH",
                             "store_id": self.stores[i]["store_id"], "business_date": clock.day_str[d]})
        for n, (d, s) in enumerate(out["po"]):
            docs.append({"doc_id": f"PO-2026-{n + 1:06d}", "doc_type": "PURCHASE_ORDER",
                         "store_id": self.stores[s]["store_id"], "business_date": clock.day_str[d]})
        m["source_documents.csv"] = docs
        m["transfers.csv"] = [{k: t[k] for k in ("transfer_id", "from_store_id", "to_store_id", "created_at",
                                                 "expected_arrival_at")} for t in self.transfers]
        m["transfer_lines.csv"] = [{"transfer_id": t["transfer_id"], "sku_id": self.skus[l["sku"]]["sku_id"],
                                    "planned_qty": l["qty"]} for t in self.transfers
                                   for l in sorted(t["lines"], key=lambda l: l["sku"])]
        return m

    def run(self) -> dict:
        self.build_masters()
        self.plan()
        self.simulate()
        self.inject_anomalies()
        self.derive_counts()
        self.top_up()
        return self.build_outputs()


GT_COLUMNS = ["gt_id", "grain", "entity_key", "movement_id", "store_id", "related_store_id", "sku_id", "scenario",
              "is_anomaly", "expected_decision", "expected_code", "correction_expected", "occurred_at", "window_start",
              "window_end", "hidden_loss_units", "miscount_units", "unrecorded_shrink_units", "explain_qty",
              "oracle_expected_qty", "oracle_counted_qty", "oracle_variance_qty", "timing_subtype",
              "late_movement_qty", "correction_posted_qty", "notes"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sql_literal(v) -> str:
    if v is None or v == "":
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def write_sql_seed(out_dir: Path, masters: dict[str, list[dict]]) -> None:
    sql_dir = out_dir / "sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    with (sql_dir / "seed_masters.sql").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("-- StashTrace master data seed (generated). Apply after `alembic upgrade head`.\n")
        fh.write("PRAGMA foreign_keys=ON;\nBEGIN;\n")
        for fname, table, cols in MASTER_FILES:
            for r in masters.get(fname, []):
                fh.write(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(sql_literal(r.get(c)) for c in cols)});\n")
        fh.write("INSERT INTO stock_balance (store_id, sku_id, on_hand, last_ledger_seq, updated_at)"
                 " SELECT store_id, sku_id, opening_qty, 0, opening_as_of FROM store_sku;\nCOMMIT;\n")
    (sql_dir / "stage_inputs.sqlite3.sql").write_text(
        "-- Load raw inputs into staging tables for ad-hoc analysis with the sqlite3 CLI:\n"
        "--   cd <dataset folder> && sqlite3 analysis.db < sql/stage_inputs.sqlite3.sql\n"
        "-- Trusted stock is NOT built from these tables; post inputs through the import API / data_tools.load.\n"
        ".mode csv\n.import movements_input.csv stg_movement_input\n.import counts_input.csv stg_count_input\n"
        "SELECT 'stg_movement_input', COUNT(*) FROM stg_movement_input;\n"
        "SELECT 'stg_count_input', COUNT(*) FROM stg_count_input;\n", encoding="utf-8")


def generate(profile: str, seed: int, out_dir: Path, quiet: bool = False) -> dict:
    import time
    t0 = time.perf_counter()
    g = Generator(profile, seed)
    out = g.run()
    masters = g.master_rows(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = {f: c for f, _, c in MASTER_FILES}
    files: dict[str, int] = {}
    for fname, rows in masters.items():
        files[fname] = write_csv(out_dir / fname, cols[fname], rows)
    files["movements_input.csv"] = write_csv(out_dir / "movements_input.csv", MOVEMENT_FIELDS, out["movements"])
    files["counts_input.csv"] = write_csv(out_dir / "counts_input.csv", COUNT_FIELDS, out["counts"])
    files["employee_roles.csv"] = write_csv(out_dir / "employee_roles.csv", ["role_code", "role_name", "default_scope"],
                                            [{"role_code": k, "role_name": v["name"], "default_scope": v["scope"]}
                                             for k, v in C.ROLES.items()])
    files["role_permissions.csv"] = write_csv(out_dir / "role_permissions.csv", ["role_code", "action"],
                                              [{"role_code": k, "action": a} for k, v in C.ROLES.items() for a in v["actions"]])
    gt = sorted(out["gt"], key=lambda r: (r["grain"], r["entity_key"]))
    for i, r in enumerate(gt):
        r["gt_id"] = f"GT-{i + 1:07d}"
    files["ground_truth.csv"] = write_csv(out_dir / "ground_truth.csv", GT_COLUMNS, gt)
    scen: dict = defaultdict(lambda: defaultdict(int))
    for r in gt:
        scen[r["grain"]][r["scenario"]] += 1
    gt_json = {
        "description": "Ground truth for evaluation only. Never an input to the application or loader.",
        "label_rules": {
            "COUNT": f"TIMING_REVIEW if the count's own timestamps contradict the ledger sequence (cutoff after "
                     f"submission, submission before the count, or stock movements between an early cutoff and the "
                     f"count); else SHRINKAGE_ALERT if planted hidden loss outstanding at the cutoff (not yet written "
                     f"off by a COUNT_CORRECTION) >= {THRESHOLD} units; else NO_ALERT. Invalid counter -> QUARANTINE.",
            "MOVEMENT": "Expected import outcome of the planted reporting anomaly (QUARANTINE with error code, "
                        "DUPLICATE, CONFLICT) or POSTED for benign corrections (POS re-ring as reversal + replacement).",
            "TRANSFER": "TRANSFER_ALERT for planted transit loss written off at both ends (double count) or left "
                        "unreconciled past expected arrival + 7 days; benign: written off once, delayed within grace, "
                        "in transit at period end, normal.",
            "ADJUSTMENT_WINDOW": "PATTERN_ALERT for planted 4-6 DAMAGE write-offs by one clerk within 12 days.",
        },
        "scenario_counts": {k: dict(sorted(v.items())) for k, v in sorted(scen.items())},
    }
    (out_dir / "ground_truth.json").write_text(json.dumps(gt_json, indent=2), encoding="utf-8")
    write_sql_seed(out_dir, masters)
    demo_logins = [g.admin, g.ops[0], g.analysts[0], g.lp[g.regions[0]],
                   g.roster[0]["STORE_MANAGER"][0][0], g.roster[0]["STOCK_CLERK"][-1][0]]
    manifest = {
        "generator": "data_tools/generate.py", "generator_version": GENERATOR_VERSION, "profile": profile,
        "seed": seed, "command": f"python -m data_tools.generate --profile {profile} --seed {seed} --out {out_dir.as_posix()}",
        "parameters": PROFILES[profile], "scenario_rates": SCENARIO_RATES, "label_threshold_units": THRESHOLD,
        "period": {"start": START.isoformat(), "days": g.days},
        "provenance": {"external_data": "none", "method": "100% synthetic; name/word lists authored for this project; "
                       "no personal or confidential data", "license": "Generated data may be freely redistributed."},
        "demo_logins": demo_logins,
        "volumes": {"movement_inputs": len(out["movements"]), "count_inputs": len(out["counts"]),
                    "count_corrections_posted_by_stores": g.corrections, "topup_delta": g.stats["topup_delta"]},
        "evaluation_only_files": ["ground_truth.csv", "ground_truth.json"],
        "files": {},
    }
    for fname in sorted(list(files) + ["ground_truth.json", "sql/seed_masters.sql", "sql/stage_inputs.sqlite3.sql"]):
        path = out_dir / fname
        manifest["files"][fname] = {"rows": files.get(fname), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not quiet:
        print(json.dumps({"profile": profile, "seed": seed, "seconds": round(time.perf_counter() - t0, 1),
                          **manifest["volumes"], "files": {k: v["rows"] for k, v in manifest["files"].items()}}, indent=2))
    return manifest


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="StashTrace synthetic data generator")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="starter")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    generate(a.profile, a.seed, Path(a.out or f"data/{a.profile}"))


if __name__ == "__main__":
    main(sys.argv[1:])
