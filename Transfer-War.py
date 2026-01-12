import argparse
import random
import re
from pathlib import Path

import pandas as pd


try:
    import pulp
except ImportError as e:
    raise SystemExit(
        "Missing dependency: PuLP.\n"
        "Install with:\n"
        "  python -m pip install pulp\n"
        "Then rerun."
    ) from e



DEFAULT_MARKET_FILE = "Top Players FC26.xlsx"
DEFAULT_SQUAD_BARCA = "Barcelona_Squad.xlsx"
DEFAULT_SQUAD_REAL = "Real_Madrid_Squad.xlsx"

DEFAULT_AVAILABILITY = 0.85


MAX_TRANSFERS_REAL = 4
MAX_TRANSFERS_BARCA = 4

BUDGET_REAL = 500
BUDGET_BARCA = 400

ROLE_SET = ["GK", "CB", "LB", "RB", "CDM", "CM", "CAM", "LW", "RW", "ST"]

# Minimum role-group coverage after transfers 
MIN_ROLE_COUNTS_REAL = {"GK": 2, "CB": 4, "FB": 2, "MID": 4, "WING": 2, "ST": 2}
MIN_ROLE_COUNTS_BARCA = {"GK": 2, "CB": 4, "FB": 2, "MID": 4, "WING": 2, "ST": 2}

# Need multipliers 
NEED_REAL = {
    "GK": 1.00, "CB": 1.35, "LB": 1.30, "RB": 1.25, "CDM": 1.25,
    "CM": 1.05, "CAM": 1.05, "LW": 1.15, "RW": 1.10, "ST": 1.00
}
NEED_BARCA = {
    "GK": 1.00, "CB": 1.30, "LB": 1.20, "RB": 1.20, "CDM": 1.20,
    "CM": 1.05, "CAM": 1.05, "LW": 1.20, "RW": 1.15, "ST": 1.25
}

# Denial weights by role 
DENIAL_VS_REAL = {   
    "GK": 0.0, "CB": 1.0, "LB": 1.0, "RB": 1.0, "CDM": 0.8,
    "CM": 0.2, "CAM": 0.2, "LW": 0.6, "RW": 0.4, "ST": 0.2
}
DENIAL_VS_BARCA = {  
    "GK": 0.0, "CB": 1.0, "LB": 0.8, "RB": 0.8, "CDM": 0.6,
    "CM": 0.2, "CAM": 0.2, "LW": 0.8, "RW": 0.6, "ST": 1.0
}


UNTOUCHABLES_BARCA = {
    "Lamine Yamal", "Pedri", "Joan García", "Joan Garcia", "Pau Cubarsí", "Pau Cubarsi",
    "Alejandro Balde", "Jules Koundé", "Jules Kounde", "Frenkie de Jong", "Frenkie De Jong",
    "Raphinha"
}
UNTOUCHABLES_REAL = {
    "Kylian Mbappé", "Kylian Mbappe", "Jude Bellingham", "Aurélien Tchouaméni", "Aurelien Tchouameni",
    "Eduardo Camavinga", "Raúl Asencio", "Raul Asencio", "Dean Huijsen"
}


FORCED_DEPARTURES_BARCA = {"Marcus Rashford"}
FORCED_DEPARTURES_REAL = set()

# Candidate reduction: keep top candidates per role (tractability)
TOP_N_PER_ROLE = 250



def norm_text(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = (s.replace("í", "i").replace("é", "e").replace("á", "a")
           .replace("ó", "o").replace("ú", "u").replace("ç", "c"))
    return s


def role_group(role: str) -> str:
    role = str(role).upper().strip()
    if role == "GK":
        return "GK"
    if role == "CB":
        return "CB"
    if role in {"LB", "RB"}:
        return "FB"
    if role in {"CDM", "CM", "CAM"}:
        return "MID"
    if role in {"LW", "RW"}:
        return "WING"
    if role == "ST":
        return "ST"
    return "MID"


def primary_role_from_position(pos: str) -> str:
    if pos is None or (isinstance(pos, float) and pd.isna(pos)):
        return "CM"
    s = str(pos).upper().strip()
    parts = re.split(r"[\/,\|\- ]+", s)
    for p in parts:
        p = p.strip()
        if p in ROLE_SET:
            return p
        if p in {"DM", "CDM"}:
            return "CDM"
        if p in {"AM", "CAM"}:
            return "CAM"
        if p in {"CF", "ST"}:
            return "ST"
        if p in {"LM"}:
            return "LW"
        if p in {"RM"}:
            return "RW"
        if p in {"LB", "LWB"}:
            return "LB"
        if p in {"RB", "RWB"}:
            return "RB"
    return "CM"


def is_real_madrid_club(club_norm: str) -> bool:
    # FC26 dataset typically uses "Real Madrid" (sometimes includes CF).
    return bool(re.search(r"\breal madrid\b", club_norm))


def is_barcelona_club(club_norm: str) -> bool:
    # Be strict enough to not block Espanyol.
    if "espanyol" in club_norm:
        return False
    return bool(re.search(r"\bfc barcelona\b", club_norm)) or club_norm.strip() == "barcelona"

def load_market(excel_path: Path) -> pd.DataFrame:
    df = pd.read_excel(excel_path)
    required = ["Player", "Club", "Position", "Estimated Price (€)", "Benefit Score (0-10)"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Market file missing columns: {missing}. Found: {list(df.columns)}")

    out = pd.DataFrame()
    out["Player"] = df["Player"].astype(str)
    out["Club"] = df["Club"].astype(str)
    out["ListedPos"] = df["Position"].astype(str)
    out["Cost_MEUR"] = pd.to_numeric(df["Estimated Price (€)"], errors="coerce")
    out["Score_0_10"] = pd.to_numeric(df["Benefit Score (0-10)"], errors="coerce")
    out = out.dropna(subset=["Cost_MEUR", "Score_0_10"]).copy()

    out["PrimaryRole"] = out["ListedPos"].apply(primary_role_from_position)
    out["__player_norm__"] = out["Player"].apply(norm_text)
    out["__club_norm__"] = out["Club"].apply(norm_text)
    return out


def load_squad(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0)
    required = ["Player", "Role", "Age", "SellValue_MEUR"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Squad file missing columns: {missing}. Found: {list(df.columns)}")
    df = df.copy()
    df["__player_norm__"] = df["Player"].apply(norm_text)
    df["Role"] = df["Role"].astype(str).str.upper().str.strip()
    df["SellValue_MEUR"] = pd.to_numeric(df["SellValue_MEUR"], errors="coerce").fillna(0.0)
    return df


def attach_squad_scores(squad: pd.DataFrame, market_all: pd.DataFrame, fallback: float = 7.0) -> pd.DataFrame:
    m = market_all[["__player_norm__", "Score_0_10"]].drop_duplicates("__player_norm__")
    out = squad.merge(m, on="__player_norm__", how="left")
    out["Score_0_10"] = out["Score_0_10"].fillna(fallback)
    return out


# ---------------------------
# Market availability + candidate reduction
# ---------------------------
def apply_random_availability(df_market: pd.DataFrame, p_available: float):
    seed = random.randrange(0, 2**31 - 1)
    rng = random.Random(seed)
    mask = [rng.random() < p_available for _ in range(len(df_market))]
    out = df_market.loc[mask].copy()
    print(f"[Market availability] p={p_available:.2f} | seed={seed} | available={len(out)}/{len(df_market)}")
    return out, seed


def preselect_candidates(df_market: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for r in ROLE_SET:
        sub = df_market[df_market["PrimaryRole"] == r].copy()
        if len(sub) == 0:
            continue
        sub = sub.sort_values(["Score_0_10", "Cost_MEUR"], ascending=[False, True]).head(TOP_N_PER_ROLE)
        parts.append(sub)
    if not parts:
        return df_market.head(0).copy()
    return pd.concat(parts, ignore_index=True)


def player_value(team: str, role: str, score: float, gamma: float) -> float:
    if team == "REAL":
        need = NEED_REAL.get(role, 1.0)
        denial = DENIAL_VS_BARCA.get(role, 0.0)
    else:
        need = NEED_BARCA.get(role, 1.0)
        denial = DENIAL_VS_REAL.get(role, 0.0)
    return score * need + gamma * score * denial


def denial_bonus(team: str, role: str, score: float, gamma: float) -> float:
    if gamma <= 0:
        return 0.0
    if team == "REAL":
        denial = DENIAL_VS_BARCA.get(role, 0.0)
    else:
        denial = DENIAL_VS_REAL.get(role, 0.0)
    return gamma * score * denial


# ---------------------------
# MILP solve
# ---------------------------
def solve_window(market_avail: pd.DataFrame, barca_squad: pd.DataFrame, real_squad: pd.DataFrame, gamma: float):
    in_squads = set(real_squad["__player_norm__"]).union(set(barca_squad["__player_norm__"]))
    market = market_avail[
        (~market_avail["__player_norm__"].isin(in_squads)) &
        (~market_avail["__club_norm__"].apply(is_real_madrid_club)) &
        (~market_avail["__club_norm__"].apply(is_barcelona_club))
    ].copy()

    cand = preselect_candidates(market).copy().reset_index(drop=True)
    if len(cand) == 0:
        return None, "No candidates after filtering", cand, real_squad, barca_squad

    cand["__name_id__"] = cand["__player_norm__"]
    name_groups = cand.groupby("__name_id__").indices

    I = list(range(len(cand)))
    role_i = cand["PrimaryRole"].tolist()
    cost_i = cand["Cost_MEUR"].tolist()
    score_i = cand["Score_0_10"].tolist()

    real_forced = {norm_text(x) for x in FORCED_DEPARTURES_REAL}
    barca_forced = {norm_text(x) for x in FORCED_DEPARTURES_BARCA}

    real = real_squad.copy()
    barca = barca_squad.copy()
    real["__forced__"] = real["__player_norm__"].isin(real_forced).astype(int)
    barca["__forced__"] = barca["__player_norm__"].isin(barca_forced).astype(int)

    prob = pulp.LpProblem("Transfer_Window_BuySell", pulp.LpMaximize)

    bR = pulp.LpVariable.dicts("buy_real", I, 0, 1, cat=pulp.LpBinary)
    bB = pulp.LpVariable.dicts("buy_barca", I, 0, 1, cat=pulp.LpBinary)

    PR = list(range(len(real)))
    PB = list(range(len(barca)))
    sR = pulp.LpVariable.dicts("sell_real", PR, 0, 1, cat=pulp.LpBinary)
    sB = pulp.LpVariable.dicts("sell_barca", PB, 0, 1, cat=pulp.LpBinary)

    # Objective: retained squad value + bought value
    def retained_terms(team: str, squad_df: pd.DataFrame, sell_vars):
        terms = []
        for p, row in squad_df.iterrows():
            if int(row["__forced__"]) == 1:
                continue
            r = row["Role"]
            sc = float(row["Score_0_10"])
            v = player_value(team, r, sc, gamma=0.0)
            terms.append(v * (1 - sell_vars[p]))
        return terms

    def buy_terms(team: str, buy_vars):
        terms = []
        for i in I:
            v = player_value(team, role_i[i], float(score_i[i]), gamma=gamma)
            terms.append(v * buy_vars[i])
        return terms

    prob += pulp.lpSum(retained_terms("REAL", real, sR) +
                       retained_terms("BARCA", barca, sB) +
                       buy_terms("REAL", bR) +
                       buy_terms("BARCA", bB))

    for i in I:
        prob += bR[i] + bB[i] <= 1, f"exclusive_row_{i}"
    for nm, idxs in name_groups.items():
        prob += pulp.lpSum([bR[i] + bB[i] for i in idxs]) <= 1, f"exclusive_name_{nm}"

    # Budgets with sell proceeds
    real_sell_value = pulp.lpSum([
        float(real.loc[p, "SellValue_MEUR"]) * sR[p]
        for p in PR if int(real.loc[p, "__forced__"]) == 0
    ])
    barca_sell_value = pulp.lpSum([
        float(barca.loc[p, "SellValue_MEUR"]) * sB[p]
        for p in PB if int(barca.loc[p, "__forced__"]) == 0
    ])
    prob += pulp.lpSum([cost_i[i] * bR[i] for i in I]) <= BUDGET_REAL + real_sell_value, "budget_real"
    prob += pulp.lpSum([cost_i[i] * bB[i] for i in I]) <= BUDGET_BARCA + barca_sell_value, "budget_barca"

    prob += pulp.lpSum([bR[i] for i in I]) <= MAX_TRANSFERS_REAL, "max_buys_real"
    prob += pulp.lpSum([bB[i] for i in I]) <= MAX_TRANSFERS_BARCA, "max_buys_barca"

    # Forced departures: must leave if listed
    for p in PR:
        if int(real.loc[p, "__forced__"]) == 1:
            prob += sR[p] == 1, f"forced_depart_real_{p}"
    for p in PB:
        if int(barca.loc[p, "__forced__"]) == 1:
            prob += sB[p] == 1, f"forced_depart_barca_{p}"

    # Untouchables cannot be sold
    untR = {norm_text(x) for x in UNTOUCHABLES_REAL}
    for p in PR:
        if int(real.loc[p, "__forced__"]) == 0 and real.loc[p, "__player_norm__"] in untR:
            prob += sR[p] == 0, f"untouch_real_{p}"
    untB = {norm_text(x) for x in UNTOUCHABLES_BARCA}
    for p in PB:
        if int(barca.loc[p, "__forced__"]) == 0 and barca.loc[p, "__player_norm__"] in untB:
            prob += sB[p] == 0, f"untouch_barca_{p}"

    # Link sells to buys: buys = sells + forced_departures
    real_forced_count = int(real["__forced__"].sum())
    barca_forced_count = int(barca["__forced__"].sum())

    prob += pulp.lpSum([bR[i] for i in I]) == pulp.lpSum([sR[p] for p in PR if int(real.loc[p, "__forced__"]) == 0]) + real_forced_count, "link_real"
    prob += pulp.lpSum([bB[i] for i in I]) == pulp.lpSum([sB[p] for p in PB if int(barca.loc[p, "__forced__"]) == 0]) + barca_forced_count, "link_barca"

    # Replace-by-group (linear): buys in group >= sells in group
    def group_buy_sum(team: str, grp: str):
        var = bR if team == "REAL" else bB
        return pulp.lpSum([var[i] for i in I if role_group(role_i[i]) == grp])

    def group_sell_sum(team: str, grp: str):
        if team == "REAL":
            squad = real
            var = sR
            P = PR
        else:
            squad = barca
            var = sB
            P = PB
        return pulp.lpSum([var[p] for p in P if int(squad.loc[p, "__forced__"]) == 0 and role_group(squad.loc[p, "Role"]) == grp])

    for grp in ["GK", "CB", "FB", "MID", "WING", "ST"]:
        prob += group_buy_sum("REAL", grp) >= group_sell_sum("REAL", grp), f"replace_real_{grp}"
        prob += group_buy_sum("BARCA", grp) >= group_sell_sum("BARCA", grp), f"replace_barca_{grp}"

    # Minimum role-group counts after transfers
    def start_count(squad: pd.DataFrame, grp: str):
        return sum(1 for r in squad["Role"].tolist() if role_group(r) == grp)

    for grp, mn in MIN_ROLE_COUNTS_REAL.items():
        start = start_count(real, grp)
        end = (start - group_sell_sum("REAL", grp) + group_buy_sum("REAL", grp))
        prob += end >= mn, f"min_real_{grp}"

    for grp, mn in MIN_ROLE_COUNTS_BARCA.items():
        start = start_count(barca, grp)
        end = (start - group_sell_sum("BARCA", grp) + group_buy_sum("BARCA", grp))
        prob += end >= mn, f"min_barca_{grp}"

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status_str = pulp.LpStatus[status]
    if status_str not in ("Optimal", "Feasible"):
        return None, status_str, cand, real, barca

    def chosen_buys(team: str):
        var = bR if team == "REAL" else bB
        return [i for i in I if pulp.value(var[i]) > 0.5]

    def chosen_sells(team: str):
        if team == "REAL":
            squad = real
            var = sR
            P = PR
        else:
            squad = barca
            var = sB
            P = PB
        out = []
        for p in P:
            if int(squad.loc[p, "__forced__"]) == 1:
                out.append(p)
            else:
                if pulp.value(var[p]) > 0.5:
                    out.append(p)
        return out

    return {
        "status": status_str,
        "objective": float(pulp.value(prob.objective)),
        "buys_real": chosen_buys("REAL"),
        "buys_barca": chosen_buys("BARCA"),
        "sells_real": chosen_sells("REAL"),
        "sells_barca": chosen_sells("BARCA"),
    }, status_str, cand, real, barca


# ---------------------------
# Output formatting
# ---------------------------
def _print_table(title: str, rows: list, headers: list[str]):
    print(title)
    print("-" * 106)
    fmt = "{:<4}  {:<28}  {:<18}  {:<8}  {:>7}  {:>6}  {:>8}"
    print(fmt.format(*headers))
    print("-" * 106)
    for r in rows:
        print(fmt.format(*r))
    print("-" * 106)


def build_sell_rows(team: str, squad: pd.DataFrame, idxs: list[int]):
    rows = []
    rev = 0.0
    kept_value_loss = 0.0
    for p in idxs:
        role = squad.loc[p, "Role"]
        player = str(squad.loc[p, "Player"])
        fee = float(squad.loc[p, "SellValue_MEUR"])
        score = float(squad.loc[p, "Score_0_10"])
        base = float(player_value(team, role, score, gamma=0.0))
        rows.append((role, player[:28], "Current", role, f"{fee:.0f}", f"{score:.1f}", f"{base:.2f}"))
        rev += fee
        kept_value_loss += base
    return rows, rev, kept_value_loss


def build_buy_rows(team: str, cand: pd.DataFrame, idxs: list[int], gamma: float):
    rows = []
    cost = 0.0
    value = 0.0
    denial = 0.0
    for i in idxs:
        role = str(cand.loc[i, "PrimaryRole"])
        player = str(cand.loc[i, "Player"])
        club = str(cand.loc[i, "Club"])
        fee = float(cand.loc[i, "Cost_MEUR"])
        score = float(cand.loc[i, "Score_0_10"])
        v = float(player_value(team, role, score, gamma=gamma))
        d = float(denial_bonus(team, role, score, gamma=gamma))
        rows.append((role, player[:28], club[:18], role, f"{fee:.0f}", f"{score:.1f}", f"{v:.2f}"))
        cost += fee
        value += v
        denial += d
    return rows, cost, value, denial


def starting_squad_value(team: str, squad: pd.DataFrame):
    total = 0.0
    for _, row in squad.iterrows():
        if int(row["__forced__"]) == 1:
            continue
        total += float(player_value(team, row["Role"], float(row["Score_0_10"]), gamma=0.0))
    return total


def print_solution_block(tag: str, res, cand, real, barca, gamma: float):
    print(f"\n{tag}")
    if res is None:
        print("No feasible solution.")
        return

    print(f"Status: {res['status']}")
    print()

    # ---------- Real ----------
    real_start = starting_squad_value("REAL", real)
    real_sell_rows, real_rev, real_lost = build_sell_rows("REAL", real, res["sells_real"])
    real_buy_rows, real_cost, real_buy_value, real_denial = build_buy_rows("REAL", cand, res["buys_real"], gamma)

    real_final = real_start - real_lost + real_buy_value
    real_delta = real_final - real_start

    print("Real Madrid")
    _print_table("Outgoing (Sells)", real_sell_rows, ["Role", "Player", "From", "Role", "Fee", "Score", "Value"])
    _print_table("Incoming (Buys)", real_buy_rows, ["Role", "Player", "Club", "Role", "Cost", "Score", "Value"])
    print(f"Budget view: sell revenue €{real_rev:.0f}M | buy cost €{real_cost:.0f}M | net spend €{(real_cost-real_rev):.0f}M")
    print(f"Strategic value (REAL): start {real_start:.2f} | end {real_final:.2f} | change {real_delta:+.2f} | denial bonus (in buys) {real_denial:.2f}")
    print()

    # ---------- Barca ----------
    barca_start = starting_squad_value("BARCA", barca)
    barca_sell_rows, barca_rev, barca_lost = build_sell_rows("BARCA", barca, res["sells_barca"])
    barca_buy_rows, barca_cost, barca_buy_value, barca_denial = build_buy_rows("BARCA", cand, res["buys_barca"], gamma)

    barca_final = barca_start - barca_lost + barca_buy_value
    barca_delta = barca_final - barca_start

    print("FC Barcelona")
    _print_table("Outgoing (Sells)", barca_sell_rows, ["Role", "Player", "From", "Role", "Fee", "Score", "Value"])
    _print_table("Incoming (Buys)", barca_buy_rows, ["Role", "Player", "Club", "Role", "Cost", "Score", "Value"])
    print(f"Budget view: sell revenue €{barca_rev:.0f}M | buy cost €{barca_cost:.0f}M | net spend €{(barca_cost-barca_rev):.0f}M")
    print(f"Strategic value (BARCA): start {barca_start:.2f} | end {barca_final:.2f} | change {barca_delta:+.2f} | denial bonus (in buys) {barca_denial:.2f}")
    print()

    combined_start = real_start + barca_start
    combined_end = real_final + barca_final
    combined_delta = combined_end - combined_start

    print(f"Combined strategic value: start {combined_start:.2f} | end {combined_end:.2f} | change {combined_delta:+.2f}")
    print(f"Combined objective value (solver): {res['objective']:.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", type=str, default=DEFAULT_MARKET_FILE)
    parser.add_argument("--barca-squad", type=str, default=DEFAULT_SQUAD_BARCA)
    parser.add_argument("--real-squad", type=str, default=DEFAULT_SQUAD_REAL)
    parser.add_argument("--availability", type=float, default=DEFAULT_AVAILABILITY)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent

    market_path = Path(args.market)
    if not market_path.is_absolute():
        market_path = script_dir / market_path

    barca_path = Path(args.barca_squad)
    if not barca_path.is_absolute():
        barca_path = script_dir / barca_path

    real_path = Path(args.real_squad)
    if not real_path.is_absolute():
        real_path = script_dir / real_path

    market_all = load_market(market_path)
    market_avail, _seed = apply_random_availability(market_all, args.availability)

    barca = attach_squad_scores(load_squad(barca_path), market_all, fallback=7.0)
    real = attach_squad_scores(load_squad(real_path), market_all, fallback=7.0)

    # Same availability for both scenarios within run
    resA, stA, candA, realA, barcaA = solve_window(market_avail, barca, real, gamma=0.0)
    print_solution_block("SCENARIO A — PEACE (gamma = 0)", resA, candA, realA, barcaA, gamma=0.0)

    resB, stB, candB, realB, barcaB = solve_window(market_avail, barca, real, gamma=0.5)
    print_solution_block("SCENARIO B — WAR (gamma = 0.5)", resB, candB, realB, barcaB, gamma=0.5)


if __name__ == "__main__":
    main()
