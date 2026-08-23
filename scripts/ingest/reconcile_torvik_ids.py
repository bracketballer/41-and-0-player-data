"""Match athlete and normalized player-season rows to Torvik defensive rows.

players was sourced from CBBD and has no Torvik id, and the two sources spell
names/teams differently ("Tennessee State" vs "Tennessee St.", "San José State"
vs "San Jose St.", "L.J." vs "LJ", "Aaron Davis III" vs "Aaron Davis",
"Bam Adebayo" vs "Edrice Adebayo"...). This resolves each player (season >= 2008)
to a Torvik player-season and stamps players.torvik_id + torvik_match_method.

Matching tiers, most-corroborated first (first hit wins):
  manual        -- hand-verified override (nickname / legal name change / name-order swap)
  exact         -- raw (name, team, season) identical
  normalized    -- normalized (name, team, season) agree, unambiguously
  team_fuzzy    -- SAME school+season, best fuzzy name match >= threshold (safe:
                   the school anchors it, so coincidental same-names elsewhere
                   can't leak in)
  team_surname  -- SAME school+season, exactly one player shares the surname
                   (catches nicknames: Bam/Edrice Adebayo, Mo/Mohamed Bamba)
  name_season   -- normalized name unique in season, team ignored (school aliases
                   like Penn/Pennsylvania, FIU/Florida International). Flagged.

The school alias map used by the team_* tiers is LEARNED from the confident
(exact/normalized/name_season) matches -- no hardcoded school list.

Players Torvik genuinely lacks (e.g. New Orleans has no 2011/2012 data) stay
unmatched; that is correct, not a bug. Unmatched + name_season rows are written
to CSVs for review.

Run after V13 is applied and player_defensive_stats is populated:
    python -m scripts.ingest.reconcile_torvik_ids
"""

import csv
import difflib
import re
import unicodedata
from collections import Counter, defaultdict

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:  # Pure matching is useful during offline bundle validation.
    psycopg2 = None

    def execute_values(*_args, **_kwargs):
        raise RuntimeError("psycopg2 is required for Torvik database reconciliation")

# Reuse the ingestion script's .env loading and DSN builder.
from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.paths import TORVIK_REPORTS

FIRST_SEASON = 2008  # Torvik's first covered season; pre-2008 stays unmatched.
UNMATCHED_CSV = TORVIK_REPORTS / "torvik_unmatched.csv"
NAME_SEASON_REVIEW_CSV = TORVIK_REPORTS / "torvik_name_season_matches.csv"
PLAYER_SEASON_UNMATCHED_CSV = TORVIK_REPORTS / "torvik_player_season_unmatched.csv"
PLAYER_SEASON_REVIEW_CSV = TORVIK_REPORTS / "torvik_player_season_name_matches.csv"

# Name suffixes to drop so "Aaron Davis III" matches "Aaron Davis".
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Minimum fuzzy ratio (on spaceless normalized names) for a same-school match,
# and the margin the best candidate must beat the runner-up by, so we never pick
# between two similarly-named teammates.
FUZZY_MIN = 0.80
FUZZY_MARGIN = 0.07

# Hand-verified matches for the handful that no rule can safely reach: nicknames,
# legal name changes, and name-order swaps. Keyed by (players.name, schools.name,
# season) -> torvik_id. Each was confirmed against Torvik (same school + season,
# and games played corroborates). Genuinely-absent players are intentionally
# NOT here -- forcing a match would corrupt the data.
MANUAL_OVERRIDES = {
    ("Josiah Moore", "Oral Roberts", 2025): 128050,    # JoJo Moore
    ("David Jones Garcia", "Memphis", 2024): 74217,     # David Jones (32 g == 32 g)
    ("Dewan Hernandez", "Miami", 2018): 46264,          # Dewan Huell (legal name change, 32 g)
    ("Michael Thomas", "Hawai'i", 2018): 28746,         # Mike Thomas
    ("Sheldon Mac", "Miami", 2016): 38559,              # Sheldon McClellan (35 g == 35 g)
    ("Vytas Sulskis", "Youngstown State", 2011): 12613,  # "Sulskis Vytas" (name-order swap)
    ("Nate Scott", "Eastern Michigan", 2022): 71203,    # Nathan Scott (a 2nd Scott on roster blocks the surname tier)
}


def strip_accents(text):
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize_name(name):
    """Normalize a player name: drop accents, punctuation, and Jr./III suffixes.

    Dots/apostrophes/commas are removed (not spaced) so "L.J." -> "lj" matches
    "LJ" and "Miller, Jr." -> "miller"; hyphens become spaces. Handles the
    unicode apostrophe (U+2019) seen in names like "Adante’".
    """
    text = strip_accents(name or "").lower()
    for ch in ".'’‘`,":
        text = text.replace(ch, "")
    text = re.sub(r"-", " ", text)
    tokens = [t for t in text.split() if t and t not in NAME_SUFFIXES]
    return " ".join(tokens)


def normalize_team(team):
    """Normalize a school name across CBBD/Torvik spellings.

    Handles State<->St., Saint<->St, drops "(PA)"-style qualifiers, "&"->"and",
    "University", accents, and punctuation.
    """
    text = strip_accents(team or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)        # drop "(PA)" etc.
    text = text.replace("&", " and ")
    text = text.replace(".", "").replace("'", "")
    text = re.sub(r"-", " ", text)
    text = re.sub(r"\bsaint\b", "st", text)
    text = re.sub(r"\bstate\b", "st", text)
    text = re.sub(r"\buniversity\b", " ", text)
    return " ".join(text.split())


def _spaceless(name):
    return normalize_name(name).replace(" ", "")


def _surname(name):
    tokens = normalize_name(name).split()
    return tokens[-1] if tokens else ""


def build_lookups(pds_rows):
    """Build exact / normalized-team / normalized-name indexes over Torvik rows.

    Each maps a key to the set of distinct torvik_ids that satisfy it, so a match
    is only accepted when the key resolves to exactly one Torvik player.
    """
    exact = {}        # (name, team, season) -> {torvik_id}
    norm_team = {}    # (nname, nteam, season) -> {torvik_id}
    norm_name = {}    # (nname, season) -> {torvik_id}
    for torvik_id, player, team, season in pds_rows:
        exact.setdefault((player, team, season), set()).add(torvik_id)
        nname, nteam = normalize_name(player), normalize_team(team)
        norm_team.setdefault((nname, nteam, season), set()).add(torvik_id)
        norm_name.setdefault((nname, season), set()).add(torvik_id)
    return exact, norm_team, norm_name


def resolve_strict(player, team, season, exact, norm_team):
    """First-pass match: exact then normalized (both team-corroborated)."""
    hit = exact.get((player, team, season))
    if hit and len(hit) == 1:
        return next(iter(hit)), "exact"
    hit = norm_team.get((normalize_name(player), normalize_team(team), season))
    if hit and len(hit) == 1:
        return next(iter(hit)), "normalized"
    return None, None


def resolve_school_anchored(player, team, season, crosswalk, by_team_season):
    """Second pass: align the school, then fuzzy/surname match within it."""
    candidates = _school_candidates(team, season, crosswalk, by_team_season)
    if not candidates:
        return None, None

    target = _spaceless(player)
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, target, _spaceless(pl)).ratio(), tid)
            for pl, tid in candidates
        ),
        reverse=True,
    )
    best_ratio, best_id = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_ratio >= FUZZY_MIN and (
        len(scored) == 1 or best_ratio - runner_up >= FUZZY_MARGIN
    ):
        return best_id, "team_fuzzy"

    surname = _surname(player)
    surname_hits = {tid for pl, tid in candidates if _surname(pl) == surname}
    if len(surname_hits) == 1:
        return next(iter(surname_hits)), "team_surname"
    return None, None


def resolve_name_season(player, season, norm_name):
    """Last resort: unique normalized name in the season, ignoring team."""
    hit = norm_name.get((normalize_name(player), season))
    if hit and len(hit) == 1:
        return next(iter(hit)), "name_season"
    return None, None


def _school_candidates(team, season, crosswalk, by_team_season):
    """Torvik (player, torvik_id) rows for the player's school that season."""
    torvik_team = crosswalk.get(team)
    if torvik_team is not None:
        cands = by_team_season.get((torvik_team, season), [])
        if cands:
            return cands
    # Fall back to normalized-team equality if the crosswalk has no entry.
    nteam = normalize_team(team)
    cands = []
    for (tm, se), rows in by_team_season.items():
        if se == season and normalize_team(tm) == nteam:
            cands.extend(rows)
    return cands


def match_all(players, pds_rows):
    """Resolve every player to (torvik_id, method) or (None, None).

    Returns the list of (player_id, torvik_id, method) matches plus per-method
    counts and the unmatched / name_season review rows.
    """
    exact, norm_team, norm_name = build_lookups(pds_rows)
    pds_team_by_id = {tid: tm for tid, pl, tm, se in pds_rows}
    by_team_season = defaultdict(list)
    for tid, pl, tm, se in pds_rows:
        by_team_season[(tm, se)].append((pl, tid))

    # Pass 1: resolve the corroborated tiers (strict + the team-agnostic
    # name_season) up front, and let BOTH vote on the CBBD-school -> Torvik-team
    # crosswalk. name_season votes are essential: schools whose CBBD spelling
    # never normalizes to Torvik's (Seattle U->Seattle, UAlbany->Albany,
    # UT Martin->Tennessee Martin) have no strict matches, so only name_season
    # reveals their alias. Majority vote shrugs off the odd wrong name_season hit.
    strict, name_season = {}, {}
    crosswalk_votes = defaultdict(Counter)
    for player_id, name, team, season in players:
        torvik_id, method = resolve_strict(name, team, season, exact, norm_team)
        if method:
            strict[player_id] = (torvik_id, method)
            crosswalk_votes[team][pds_team_by_id[torvik_id]] += 1
            continue
        torvik_id, method = resolve_name_season(name, season, norm_name)
        if method:
            name_season[player_id] = torvik_id
            crosswalk_votes[team][pds_team_by_id[torvik_id]] += 1
    crosswalk = {
        team: votes.most_common(1)[0][0] for team, votes in crosswalk_votes.items()
    }

    matches, unmatched, name_season_rows = [], [], []
    counts = Counter()
    for player_id, name, team, season in players:
        override = MANUAL_OVERRIDES.get((name, team, season))
        if override is not None:
            torvik_id, method = override, "manual"
        elif player_id in strict:
            torvik_id, method = strict[player_id]
        else:
            # Prefer same-school matching (now that the crosswalk is complete);
            # fall back to the team-agnostic name_season hit from pass 1.
            torvik_id, method = resolve_school_anchored(
                name, team, season, crosswalk, by_team_season
            )
            if method is None and player_id in name_season:
                torvik_id, method = name_season[player_id], "name_season"

        if method is None:
            unmatched.append((player_id, name, team, season))
            continue
        counts[method] += 1
        matches.append((player_id, torvik_id, method))
        if method == "name_season":
            name_season_rows.append((player_id, name, team, season, torvik_id))

    return matches, counts, unmatched, name_season_rows


def main():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required for Torvik database reconciliation")
    TORVIK_REPORTS.mkdir(parents=True, exist_ok=True)
    load_env_file()
    conn = psycopg2.connect(connection_dsn())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.name, s.name, p.season
                FROM players p
                JOIN schools s ON s.id = p.school_id
                WHERE p.season >= %s
                """,
                (FIRST_SEASON,),
            )
            players = cur.fetchall()
            cur.execute(
                "SELECT torvik_id, player, team, season FROM player_defensive_stats"
            )
            pds_rows = cur.fetchall()
            cur.execute(
                """
                SELECT ps.id, p.name, s.name, ps.season
                FROM player_seasons ps
                JOIN players p ON p.id = ps.player_id
                JOIN schools s ON s.id = ps.team_id
                WHERE ps.season >= %s
                """,
                (FIRST_SEASON,),
            )
            player_seasons = cur.fetchall()

        matches, counts, unmatched, name_season_rows = match_all(players, pds_rows)
        (
            season_matches,
            season_counts,
            season_unmatched,
            season_name_rows,
        ) = match_all(player_seasons, pds_rows)

        # Idempotent: clear prior matches, then write the fresh ones.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE players SET torvik_id = NULL, torvik_match_method = NULL "
                "WHERE torvik_id IS NOT NULL OR torvik_match_method IS NOT NULL"
            )
            execute_values(
                cur,
                """
                UPDATE players p
                SET torvik_id = v.torvik_id, torvik_match_method = v.method
                FROM (VALUES %s) AS v(player_id, torvik_id, method)
                WHERE p.id = v.player_id
                """,
                matches,
                template="(%s, %s, %s)",
                page_size=500,
            )
            cur.execute(
                """
                UPDATE player_seasons
                SET torvik_id = NULL, torvik_match_method = NULL,
                    updated_at = now()
                WHERE torvik_id IS NOT NULL OR torvik_match_method IS NOT NULL
                """
            )
            if season_matches:
                execute_values(
                    cur,
                    """
                    UPDATE player_seasons ps
                    SET torvik_id = v.torvik_id,
                        torvik_match_method = v.method,
                        updated_at = now()
                    FROM (VALUES %s) AS v(
                        player_season_id, torvik_id, method
                    )
                    WHERE ps.id = v.player_season_id
                    """,
                    season_matches,
                    template="(%s, %s, %s)",
                    page_size=500,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _write_csv(UNMATCHED_CSV, ["player_id", "name", "team", "season"], unmatched)
    _write_csv(
        NAME_SEASON_REVIEW_CSV,
        ["player_id", "name", "team", "season", "torvik_id"],
        name_season_rows,
    )
    _write_csv(
        PLAYER_SEASON_UNMATCHED_CSV,
        ["player_season_id", "name", "team", "season"],
        season_unmatched,
    )
    _write_csv(
        PLAYER_SEASON_REVIEW_CSV,
        ["player_season_id", "name", "team", "season", "torvik_id"],
        season_name_rows,
    )

    total = len(players)
    matched = sum(counts.values())
    print(f"players (season >= {FIRST_SEASON}): {total}")
    for method in (
        "exact", "normalized", "team_fuzzy", "team_surname", "name_season", "manual"
    ):
        print(f"  {method:<13}{counts[method]}")
    print(f"  {'matched':<13}{matched} ({100 * matched / total:.1f}%)")
    print(f"  {'unmatched':<13}{len(unmatched)}")
    season_total = len(player_seasons)
    season_matched = sum(season_counts.values())
    print(f"player_seasons (season >= {FIRST_SEASON}): {season_total}")
    for method in (
        "exact",
        "normalized",
        "team_fuzzy",
        "team_surname",
        "name_season",
        "manual",
    ):
        print(f"  {method:<13}{season_counts[method]}")
    season_pct = 100 * season_matched / season_total if season_total else 0
    print(f"  {'matched':<13}{season_matched} ({season_pct:.1f}%)")
    print(f"  {'unmatched':<13}{len(season_unmatched)}")
    print(f"Review files: {UNMATCHED_CSV}, {NAME_SEASON_REVIEW_CSV}")


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


if __name__ == "__main__":
    main()
