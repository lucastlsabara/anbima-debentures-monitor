"""Poda data/b3_trades/ e data/b3_trades_consolidated/ mantendo apenas
os ultimos 60 dias uteis B3 no main.

Politica de retencao (D6 do audit-report):
  - main: 60 dias uteis B3 contados a partir de hoje (rolling)
  - branch `historical-data`: backup completo do estado historico,
    nao recebe builds futuros (so retem snapshots brutos).

Le os manifests de data/b3_trades/manifest.json e
data/b3_trades_consolidated/manifest.json, identifica datas mais
antigas que o cutoff e:
  - deleta o JSON correspondente
  - atualiza o manifest removendo a entrada

NAO toca em:
  - data/history/        (ANBIMA snapshots — retencao total, sao pequenos)
  - data/dispersion/     (idem)
  - data/overview.json, data/movements.json, data/manifest.json, etc

CLI:
  python scripts/podar_historico.py        # aplica
  python scripts/podar_historico.py --dry  # so reporta o que seria podado
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from b3_calendar import is_b3_business_day, today_brt  # noqa: E402


RETENTION_BUSINESS_DAYS = 60

DATASETS = [
    ROOT / "data" / "b3_trades",
    ROOT / "data" / "b3_trades_consolidated",
]


def cutoff_date(n: int = RETENTION_BUSINESS_DAYS, today: date | None = None) -> date:
    """Data limite: ultimo dia util B3 contando N para tras a partir de hoje
    (exclusive). Datas estritamente anteriores serao podadas.
    """
    if today is None:
        today = today_brt()
    d = today
    count = 0
    while count < n:
        d -= timedelta(days=1)
        if is_b3_business_day(d):
            count += 1
    return d


def prune_dataset(dataset_dir: Path, cutoff: date, *, dry: bool) -> tuple[int, int]:
    """Remove arquivos < cutoff e atualiza o manifest. Retorna (n_arquivos,
    n_dates_no_manifest_apos)."""
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"[{dataset_dir.name}] sem manifest, pulando")
        return (0, 0)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dates = manifest.get("dates") or []

    keep: list[dict] = []
    to_delete: list[Path] = []
    for entry in dates:
        d_iso = entry["date"]
        if date.fromisoformat(d_iso) < cutoff:
            fname = entry.get("filename") or f"{d_iso}.json"
            to_delete.append(dataset_dir / fname)
        else:
            keep.append(entry)

    if not to_delete:
        print(f"[{dataset_dir.name}] nada a podar (cutoff={cutoff.isoformat()})")
        return (0, len(keep))

    print(
        f"[{dataset_dir.name}] cutoff={cutoff.isoformat()}: "
        f"{len(to_delete)} arquivo(s) a podar, {len(keep)} mantido(s)"
    )
    for p in to_delete:
        marker = "  (dry) " if dry else "  "
        print(f"{marker}rm {p.relative_to(ROOT)}")
        if not dry and p.exists():
            p.unlink()

    if not dry:
        # Recalcula totais a partir das entradas mantidas.
        new_manifest = {**manifest, "dates": keep}
        new_manifest["earliest_date"] = keep[0]["date"] if keep else None
        new_manifest["latest_date"] = keep[-1]["date"] if keep else None
        if "total_trades" in manifest:
            new_manifest["total_trades"] = sum(e["n_trades"] for e in keep)
        if "total_vol_brl" in manifest:
            new_manifest["total_vol_brl"] = sum(e["vol_brl"] for e in keep)
        if "total_rows" in manifest:
            new_manifest["total_rows"] = sum(e["n_rows"] for e in keep)
        manifest_path.write_text(
            json.dumps(new_manifest, separators=(",", ":")), encoding="utf-8"
        )

    return (len(to_delete), len(keep))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument(
        "--retention", type=int, default=RETENTION_BUSINESS_DAYS,
        help=f"dias uteis B3 a reter (default {RETENTION_BUSINESS_DAYS})"
    )
    args = parser.parse_args(argv[1:])

    cutoff = cutoff_date(n=args.retention)
    print(f"Retencao: {args.retention} du B3. Cutoff (exclusivo): {cutoff.isoformat()}")
    print()

    total_pruned = 0
    for ds in DATASETS:
        n_pruned, _ = prune_dataset(ds, cutoff, dry=args.dry)
        total_pruned += n_pruned
        print()

    suffix = " (dry-run)" if args.dry else ""
    print(f"Resumo{suffix}: {total_pruned} arquivo(s) podado(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
