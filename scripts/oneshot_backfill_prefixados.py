"""One-shot retroativo: aplica backfill de spread_pp para Prefixados em
todos os snapshots existentes em history/*.json e persiste de volta.

Motivo: snapshots gerados antes do item 12 (ETTJ Pré interp) tem
spread_pp=None para todo o grupo Prefixado. O build_dashboard.py
aplicava esse backfill on-the-fly toda execucao (`_backfill_prefixados`).
Apos rodar este script, os snapshots passam a ter o spread_pp persistido
e a funcao on-the-fly pode ser removida.

Idempotente: papeis que ja tem spread_pp computado nao sao tocados.

CLI:
  python scripts/oneshot_backfill_prefixados.py        # roda em todos os history/*.json
  python scripts/oneshot_backfill_prefixados.py --dry  # so reporta o que mudaria
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from compute_spreads import build_spline, indexador_group  # noqa: E402


HIST_DIR = ROOT / "history"


def backfill_snap(snap: dict) -> int:
    """Aplica backfill in-place. Retorna numero de papeis atualizados."""
    pre_rows = snap.get("ettj_pre") or [
        {"vertice_du": r["vertice_du"], "taxa_pre": r.get("taxa_pref")}
        for r in (snap.get("ettj_ipca") or [])
        if r.get("taxa_pref") is not None
    ]
    curve = build_spline(pre_rows, "taxa_pre")
    if curve is None:
        return 0
    updated = 0
    for d in snap.get("debentures", []):
        if indexador_group(d.get("indice")) != "Prefixado":
            continue
        if d.get("spread_pp") is not None:
            continue
        taxa = d.get("taxa_indicativa")
        dur = d.get("duration_dias")
        if taxa is None or dur is None or d.get("flag_iliquido"):
            continue
        ref_pre = float(curve(dur))
        sp = ((1 + taxa / 100.0) / (1 + ref_pre / 100.0) - 1) * 100.0
        d["spread_pp"] = round(sp, 4)
        d["taxa_benchmark"] = round(ref_pre, 4)
        d["benchmark_titulo"] = "ETTJ Pré"
        d["spread_metodo"] = "ettj_pre_interp"
        updated += 1
    return updated


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args(argv[1:])

    paths = sorted(HIST_DIR.glob("*.json"))
    if not paths:
        print(f"Nenhum snapshot em {HIST_DIR}")
        return 0

    total_files_touched = 0
    total_updated = 0
    for p in paths:
        snap = json.loads(p.read_text(encoding="utf-8"))
        n = backfill_snap(snap)
        if n > 0:
            total_files_touched += 1
            total_updated += n
            print(f"[{p.name}] {n} papel(eis) com spread_pp preenchido")
            if not args.dry:
                # Preserva formato pretty-printed dos snapshots de history/,
                # consistente com compute_spreads.py (indent=2).
                p.write_text(
                    json.dumps(snap, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        else:
            print(f"[{p.name}] nada a fazer (idempotente)")

    suffix = " (dry-run)" if args.dry else ""
    print()
    print(
        f"Resumo{suffix}: {total_files_touched} arquivo(s) afetado(s), "
        f"{total_updated} papel(eis) atualizado(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
