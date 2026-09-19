"""
import_historical.py — Importa años faltantes de datos CH en lamal-db existente.

Uso (dentro del lamal-comparator container o con acceso al DB):
  python3 import_historical.py --csv /path/to/lamal.csv --years 2021 2022 2023 2024 2025

El CSV generado por process.py tiene columnas:
  (index), Year, Versicherer, Kanton, Hoheitsgebiet, Region,
  Altersklasse, Unfalleinschluss, Franchise, Prime,
  isBaseP, isBaseF, Altersuntergruppe, Tarifbezeichnung, Tariftyp, Tarif

Las mapea a la tabla lamal:
  year, assuranceId, canton, pays, region, age3,
  accident, franchise, prime, isBaseP, isBaseF,
  age, tarifDesc, tarifTyp, tarif
"""

import argparse
import csv
import os
import sys

import pymysql


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True, help="Ruta al lamal.csv generado por process.py")
    p.add_argument("--years", nargs="+", type=int, default=[2021, 2022, 2023, 2024, 2025])
    p.add_argument("--pays", default="CH", help="Filtro por Hoheitsgebiet (CH o EU)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--host", default=os.getenv("DB_HOST", "localhost"))
    p.add_argument("--user", default=os.getenv("DB_USER", "lamal"))
    p.add_argument("--password", default=os.getenv("DB_PASS", "lamal"))
    p.add_argument("--database", default=os.getenv("DB_NAME", "lamal"))
    return p.parse_args()


def load_csv(path, years_set, pays_filter):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                year = int(row.get("Year", 0))
            except ValueError:
                continue
            if year not in years_set:
                continue
            if row.get("Hoheitsgebiet", "").strip() != pays_filter:
                continue
            rows.append(row)
    return rows


def coerce_int(v, default=0):
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


def coerce_float(v, default=0.0):
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def build_record(row):
    return (
        coerce_int(row.get("Year")),
        coerce_int(row.get("Versicherer")),          # assuranceId
        row.get("Kanton", "").strip(),               # canton
        row.get("Hoheitsgebiet", "").strip(),        # pays
        coerce_int(row.get("Region")),               # region
        row.get("Altersklasse", "").strip(),         # age3
        coerce_int(row.get("Unfalleinschluss")),     # accident
        coerce_int(row.get("Franchise")),            # franchise
        coerce_float(row.get("Prime")),              # prime
        coerce_int(row.get("isBaseP")),
        coerce_int(row.get("isBaseF")),
        row.get("Altersuntergruppe", "").strip(),    # age
        row.get("Tarifbezeichnung", "").strip(),     # tarifDesc
        row.get("Tariftyp", "").strip(),             # tarifTyp
        row.get("Tarif", "").strip(),                # tarif
    )


INSERT_SQL = """
INSERT INTO lamal
  (year, assuranceId, canton, pays, region, age3,
   accident, franchise, prime, isBaseP, isBaseF,
   age, tarifDesc, tarifTyp, tarif)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def main():
    args = parse_args()
    years_set = set(args.years)
    pays = args.pays

    print(f"Leyendo {args.csv} → filtrando años {sorted(years_set)}, pays={pays}")
    rows = load_csv(args.csv, years_set, pays)
    print(f"  {len(rows):,} filas encontradas")

    if not rows:
        print("Nada que importar.")
        return

    if args.dry_run:
        print("Dry-run: primera fila de muestra:")
        print(build_record(rows[0]))
        return

    db = pymysql.connect(
        host=args.host, user=args.user, password=args.password,
        database=args.database, autocommit=False,
    )
    try:
        with db.cursor() as cur:
            # Verificar que no haya datos duplicados ya
            cur.execute(
                "SELECT COUNT(*) FROM lamal WHERE year IN %s AND pays=%s",
                (tuple(sorted(years_set)), pays)
            )
            existing = cur.fetchone()[0]
            if existing:
                print(f"AVISO: Ya existen {existing:,} filas para esos años/pays. Abortando.")
                print("  Si quieres reimportar, ejecuta primero:")
                print(f"  DELETE FROM lamal WHERE year IN {tuple(sorted(years_set))} AND pays='{pays}';")
                sys.exit(1)

            batch = [build_record(r) for r in rows]
            print(f"Insertando {len(batch):,} filas…")
            cur.executemany(INSERT_SQL, batch)
        db.commit()
        print("✅ Importación completada.")
    except Exception as e:
        db.rollback()
        print(f"❌ Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
