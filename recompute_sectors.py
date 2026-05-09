"""Recalcula o campo `setor` em todos os arquivos data/b3_trades/*.json.

Para cada row, aplica a regra: se instrument == "DEB", usa
sectors.classify(ticker, issuer); caso contrario "Outros". Sobrescreve
o JSON no mesmo formato colunar minificado.

Idempotente, sem rede. Use apos ajustar a logica em sectors.py para
reaplicar a classificacao em snapshots ja persistidos.
"""

from __future__ import annotations

import json
from pathlib import Path

import sectors


DATA_DIR = Path(__file__).parent / "data" / "b3_trades"


def _recompute_file(path: Path) -> tuple[int, int]:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    try:
        i_inst = columns.index("instrument")
        i_issuer = columns.index("issuer")
        i_ticker = columns.index("ticker")
        i_setor = columns.index("setor")
    except ValueError as exc:
        raise RuntimeError(f"{path.name}: coluna ausente ({exc})")

    changed = 0
    for row in rows:
        instrument = row[i_inst]
        issuer = row[i_issuer]
        ticker = row[i_ticker]
        if instrument == "DEB":
            new_setor = sectors.classify(ticker, issuer)
        else:
            new_setor = "Outros"
        if row[i_setor] != new_setor:
            row[i_setor] = new_setor
            changed += 1

    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return len(rows), changed


def main() -> int:
    if not DATA_DIR.exists():
        print(f"Diretorio nao encontrado: {DATA_DIR}")
        return 1

    files = sorted(p for p in DATA_DIR.glob("*.json") if p.name != "manifest.json")
    if not files:
        print("Nenhum arquivo data/b3_trades/*.json (alem de manifest.json).")
        return 0

    total_rows = 0
    total_changed = 0
    for p in files:
        n, ch = _recompute_file(p)
        total_rows += n
        total_changed += ch
        print(f"  {p.name}: {n:,} rows, {ch:,} alterada(s)".replace(",", "."))

    print()
    print(
        f"OK: {len(files)} arquivo(s), "
        f"{total_rows:,} rows, {total_changed:,} setor(es) recalculado(s)".replace(",", ".")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
