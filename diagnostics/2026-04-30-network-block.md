# Diagnóstico de rede — rotina diária 30/04/2026

**Data da execução:** 2026-05-01 ~04:40 UTC (01:40 BRT)
**Branch:** `claude/update-debentures-routine-EeYWr`
**Status:** ❌ Rotina **não executada** — sandbox de rede bloqueando hosts da ANBIMA.

---

## Sintoma

Toda requisição HTTP(S) para os hosts da ANBIMA (e correlatos) retorna **HTTP 403** com header `x-deny-reason: host_not_allowed`, vindo do proxy de egress do sandbox (TLS interceptado por `sandbox-egress-production TLS Inspection CA`).

Saída de `curl` (resumida):

```
www.anbima.com.br      -> HTTP 403  x-deny-reason: host_not_allowed
anbima.com.br          -> HTTP 403  x-deny-reason: host_not_allowed
data.anbima.com.br     -> HTTP 403  x-deny-reason: host_not_allowed
api.anbima.com.br      -> HTTP 403  x-deny-reason: host_not_allowed
www.debentures.com.br  -> HTTP 403  x-deny-reason: host_not_allowed
```

Controles (mesma sessão):

```
github.com   -> HTTP 200          (allowlist base inclui GitHub)
example.com  -> HTTP 403  host_not_allowed   (default-deny confirmado)
```

WebFetch também falha com `Request failed with status code 403` para a mesma URL.

## Configuração presente no repo

`.claude/settings.json` (mergeado em `main` via PR #2):

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "sandbox": {
    "network": {
      "allowedDomains": [
        "www.anbima.com.br",
        "anbima.com.br",
        "data.anbima.com.br",
        "api.anbima.com.br",
        "www.debentures.com.br"
      ]
    }
  },
  "permissions": {
    "allow": [
      "WebFetch(domain:www.anbima.com.br)",
      "WebFetch(domain:data.anbima.com.br)",
      "WebFetch(domain:api.anbima.com.br)",
      "WebFetch(domain:www.debentures.com.br)"
    ]
  }
}
```

Apesar disso, o proxy continua negando os hosts. O bloco `permissions.allow` libera o **tool gate** do `WebFetch` (sem prompt de permissão), mas não altera a allowlist do proxy de egress.

## Hipóteses

1. **Allowlist do sandbox lida apenas no boot da sessão.** O PR #2 já alertava: a config só passa a valer em sessões iniciadas *após* o merge. Esta sessão pode ter sido iniciada antes ou pode não recarregar `.claude/settings.json` para fins de rede.
2. **Allowlist de rede não é configurável em escopo de projeto.** Pode ser que `sandbox.network.allowedDomains` em `.claude/settings.json` (project) seja ignorado e só valha em escopo `user` (`~/.claude/settings.json`) ou em config do runner de Routines.
3. **Schema/caminho da chave incorreto.** Possível que a chave correta seja `sandbox.allowedDomains` ou `network.allowedDomains` (sem `sandbox.` antes), ou outro nome esperado pelo runner.

## Ações sugeridas (fora desta sessão)

- [ ] Reiniciar a Routine em uma sessão nova após confirmar que `.claude/settings.json` está em `main`.
- [ ] Se persistir, mover a allowlist para `~/.claude/settings.json` (user scope) ou para a config do runner de Routines.
- [ ] Validar a chave correta no schema (`https://json.schemastore.org/claude-code-settings.json`) — possivelmente `sandbox.allowedDomains` ao invés de `sandbox.network.allowedDomains`.
- [ ] Como fallback, considerar host alternativo (mirror ANBIMA via API autenticada `data.anbima.com.br`) se a allowlist de `www.anbima.com.br` não for liberável.

## Por que não rodei com dados simulados

A primeira tentativa (PR #1, fechado/não-mergeado) gerou um snapshot **simulado** (`"simulado": true` no meta). Como o objetivo do repo é monitorar o mercado **real**, publicar mais um dashboard simulado mascararia o problema de rede e poluiria o histórico (`history/2026-04-30.json`) com dados que não correspondem ao fechamento real. Optei por parar e documentar.
