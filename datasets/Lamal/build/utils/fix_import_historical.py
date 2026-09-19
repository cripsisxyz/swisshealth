"""
fix_import_historical.py — Downloads 2021-2025 ZIP archives from opendata.bagnet.ch,
extracts Prämien_CH.csv using Python zipfile (reliable UTF-8 handling), applies the
same transformations as prime.py, and imports into the existing lamal-db.

Usage on frontier:
  pip install pymysql  # if not already installed
  python3 fix_import_historical.py [--dry-run] [--years 2021 2022 2023 2024 2025]

Requires access to lamal-db on 127.0.0.1:3306 (Docker port exposed on host).
"""

import argparse
import io
import os
import sys
import urllib.request
import zipfile

import pymysql

ARCHIVE_URLS = {
    2021: "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL0FyY2hpdl9QcmFlbWllbl8yMDIxLnppcA%3D%3D",
    2022: "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL0FyY2hpdl9QcmFlbWllbl8yMDIyLnppcA%3D%3D",
    2023: "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL0FyY2hpdl9QcmFlbWllbl8yMDIzLnppcA%3D%3D",
    2024: "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL0FyY2hpdl9QcmFlbWllbl8yMDI0LnppcA%3D%3D",
    2025: "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL0FyY2hpdl9QcmFlbWllbl8yMDI1LnppcA%3D%3D",
}

REGION_MAP = {
    "PR-REG CH0": "0", "PR-REG CH1": "1", "PR-REG CH2": "2", "PR-REG CH3": "3",
}
ALTERSKLASSE_MAP = {
    "AKL-KIN": "KIN", "AKL-JUG": "JUG", "AKL-ERW": "ERW",
    "0": "KIN", "19": "JUG", "26": "ERW",
}
UNFALL_MAP = {"MIT-UNF": "1", "OHN-UNF": "0", "05": "1", "06": "0"}
FRANCHISE_MAP = {
    "KIN": "0", "JUG": "300", "ERW": "300",
    "FRA-0": "0", "FRA-100": "100", "FRA-200": "200", "FRA-300": "300",
    "FRA-400": "400", "FRA-500": "500", "FRA-600": "600",
    "FRA-1000": "1000", "FRA-1500": "1500", "FRA-2000": "2000", "FRA-2500": "2500",
}
ALTER_SUBGROUP_MAP = {"JUG": "J1", "ERW": "E1"}
TARIFTYP_MAP = {"DIV": "TAR-DIV", "Base": "TAR-BASE", "BASE": "TAR-BASE",
                "HAM_RDS": "TAR-HAM", "HMO": "TAR-HMO"}

INSERT_SQL = """
INSERT INTO lamal
  (id, year, assuranceId, canton, pays, region, age3,
   accident, franchise, prime, isBaseP, isBaseF,
   age, tarifDesc, tarifTyp, tarif)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def download_zip(year, url):
    print(f"  Downloading {year} archive...", end=" ", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    print(f"{len(data) // 1024 // 1024}MB")
    return data


def find_praemien_ch(zf):
    """Find Prämien_CH.csv inside ZIP, handling encoding variants."""
    for name in zf.namelist():
        lower = name.lower()
        # Match: Prämien_CH.csv, Praemien_CH.csv, Praemien_ch.csv etc.
        if ("mien_ch" in lower or "mien_ch" in lower) and lower.endswith(".csv"):
            # Exclude Versichertenbestand
            if "versicherten" not in lower and "bestand" not in lower:
                return name
    # Fallback: any CSV with CH and not Versichertenbestand
    for name in zf.namelist():
        lower = name.lower()
        if "_ch" in lower and lower.endswith(".csv") and "versicherten" not in lower and "bestand" not in lower:
            return name
    return None


def read_csv_from_zip(zip_bytes, year):
    """Extract and parse Prämien_CH.csv from ZIP bytes."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = find_praemien_ch(zf)
        if not name:
            raise ValueError(f"Prämien_CH.csv not found in archive. Files: {zf.namelist()}")
        print(f"  Found: {name}")
        raw = zf.read(name)

    # Detect encoding (strip BOM if present)
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("iso-8859-1")

    lines = text.splitlines()
    if not lines:
        raise ValueError("Empty CSV")

    # Detect separator
    header = lines[0]
    sep = ";" if ";" in header else ","
    cols = header.split(sep)
    print(f"  Columns ({len(cols)}): {cols[:5]}...")

    if len(cols) <= 4:
        raise ValueError(f"Archive contains enrollment data (4 cols), not premium data. Cols: {cols}")

    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(sep)
        if len(parts) < len(cols):
            parts += [""] * (len(cols) - len(parts))
        rows.append(dict(zip(cols, parts)))

    return rows


def clean_val(row, *keys, default=""):
    for k in keys:
        v = row.get(k, "").strip()
        if v:
            return v
    return default


def transform_rows(raw_rows, year, id_start=0):
    """Apply prime.py transformations to raw CSV rows."""
    records = []
    next_id = id_start
    for r in raw_rows:
        region = REGION_MAP.get(clean_val(r, "Region"), clean_val(r, "Region", default="0"))
        age3 = ALTERSKLASSE_MAP.get(clean_val(r, "Altersklasse"), clean_val(r, "Altersklasse"))
        accident = UNFALL_MAP.get(clean_val(r, "Unfalleinschluss"), "0")

        franchise_raw = clean_val(r, "Franchise")
        franchise = FRANCHISE_MAP.get(franchise_raw, franchise_raw)

        prime_raw = clean_val(r, "Prämie", "Prime", "P")
        try:
            prime = float(prime_raw)
        except (ValueError, TypeError):
            continue  # skip rows with invalid prime

        is_base_p = clean_val(r, "isBaseP", "isBASE_P", default="0")
        is_base_f = clean_val(r, "isBaseF", "isBASE_F", default="0")

        age_sub_raw = clean_val(r, "Altersuntergruppe", "V2_ID")
        if not age_sub_raw:
            age_sub_raw = age3
        age = ALTER_SUBGROUP_MAP.get(age_sub_raw, age_sub_raw)

        tarif_desc = clean_val(r, "Tarifbezeichnung", "V_KBEZ")
        tarif_typ_raw = clean_val(r, "Tariftyp", "Tarif-Typ", "V_TYP")
        tarif_typ = TARIFTYP_MAP.get(tarif_typ_raw, tarif_typ_raw)
        tarif = clean_val(r, "Tarif")
        if not tarif:
            tarif = tarif_desc

        try:
            franchise_int = int(float(franchise))
        except (ValueError, TypeError):
            franchise_int = 0
        try:
            accident_int = int(accident)
        except (ValueError, TypeError):
            accident_int = 0
        try:
            is_base_p_int = int(float(is_base_p))
        except (ValueError, TypeError):
            is_base_p_int = 0
        try:
            is_base_f_int = int(float(is_base_f))
        except (ValueError, TypeError):
            is_base_f_int = 0
        try:
            region_int = int(float(region))
        except (ValueError, TypeError):
            region_int = 0
        try:
            assurance_id = int(float(clean_val(r, "Versicherer", "G_ID")))
        except (ValueError, TypeError):
            assurance_id = 0

        canton = clean_val(r, "Kanton", "C_ID", "Land")
        pays = clean_val(r, "Hoheitsgebiet", "C_GRP", default="CH")

        records.append((
            next_id, year, assurance_id, canton, pays, region_int, age3,
            accident_int, franchise_int, round(prime, 4),
            is_base_p_int, is_base_f_int,
            age, tarif_desc, tarif_typ, tarif,
        ))
        next_id += 1

    return records, next_id


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--years", nargs="+", type=int, default=[2021, 2022, 2023, 2024, 2025])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--host", default=os.getenv("DB_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    p.add_argument("--user", default=os.getenv("DB_USER", "lamal"))
    p.add_argument("--password", default=os.getenv("DB_PASS", "lamal"))
    p.add_argument("--database", default=os.getenv("DB_NAME", "lamal"))
    return p.parse_args()


def main():
    args = parse_args()

    db = None
    next_id = 0
    if not args.dry_run:
        print(f"Connecting to {args.host}:{args.port}/{args.database}...")
        db = pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            database=args.database, autocommit=False,
        )
        with db.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), -1) + 1 FROM lamal")
            next_id = cur.fetchone()[0]
        print(f"Starting id sequence from {next_id}")

    for year in sorted(args.years):
        if year not in ARCHIVE_URLS:
            print(f"No URL for {year}, skipping")
            continue

        print(f"\n=== {year} ===")

        if db:
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM lamal WHERE year=%s AND pays='CH'", (year,))
                existing = cur.fetchone()[0]
            if existing:
                print(f"  Already have {existing:,} CH rows for {year}, skipping")
                continue

        try:
            zip_bytes = download_zip(year, ARCHIVE_URLS[year])
            raw_rows = read_csv_from_zip(zip_bytes, year)
            print(f"  Parsed {len(raw_rows):,} raw rows")

            records, next_id = transform_rows(raw_rows, year, id_start=next_id)
            print(f"  Transformed {len(records):,} records")

            if args.dry_run:
                if records:
                    print(f"  Sample: {records[0]}")
                continue

            with db.cursor() as cur:
                cur.executemany(INSERT_SQL, records)
            db.commit()
            print(f"  ✅ Inserted {len(records):,} rows for {year}")

        except Exception as e:
            if db:
                db.rollback()
            print(f"  ❌ Error for {year}: {e}")
            import traceback; traceback.print_exc()

    if db:
        db.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
