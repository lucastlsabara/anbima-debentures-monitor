# Estudo: timing entre a divulgação de resultado na CVM (PDF) e a disponibilidade dos dados estruturados para importação

**Data do estudo**: 2026-07-16
**Pergunta**: a partir do momento em que uma empresa divulga o resultado na CVM
(normalmente em PDF), em quanto tempo os dados já estão disponíveis para um
importador automatizado? É ao mesmo tempo? Ou a empresa primeiro divulga o PDF
e dias depois divulga os arquivos estruturados? Qual o padrão de comportamento?

**Contexto do repositório**: hoje o monitor consome apenas ANBIMA (taxas
indicativas) e B3 (trades). Não existe importador CVM. Este estudo mapeia a
cadeia de divulgação da CVM para embasar um eventual importador de fundamentos
(receita, EBITDA, dívida líquida, alavancagem dos emissores de debêntures).

---

## TL;DR

1. **O PDF e os dados estruturados são protocolados juntos, no mesmo dia** —
   não existe o padrão "PDF hoje, arquivos dias depois" *do lado da empresa*.
   O press-release (PDF, via categoria IPE "Dados Econômico-Financeiros") e o
   formulário estruturado ITR/DFP entram pelo **mesmo sistema**
   (Empresas.NET/ENET) e, na prática de mercado, na **mesma noite** — em geral
   com minutos ou poucas horas de diferença.
2. **O atraso que existe é do lado do consumidor, e depende da rota de
   importação**:
   - **Rota tempo real (D+0, minutos)**: consulta direta ao RAD/ENET
     (`rad.cvm.gov.br`) ou ao espelho da B3. O documento fica público
     imediatamente após o protocolo — PDF e ITR estruturado ao mesmo tempo.
   - **Rota dados abertos (D+0 a D+7)**: os ZIPs anuais do portal
     `dados.cvm.gov.br` (`itr_cia_aberta_YYYY.zip`, `dfp_cia_aberta_YYYY.zip`,
     `ipe_cia_aberta_YYYY.zip`) têm **atualização semanal** do ano corrente,
     segundo a documentação oficial do dataset. Um resultado protocolado logo
     após a última regeneração do ZIP só aparece no arquivo até ~7 dias depois.
3. **Padrão de comportamento**: companhias listadas (Categoria A) divulgam
   release + ITR simultaneamente após o fechamento do pregão; emissores
   "só-debêntures" (Categoria B, SPEs de infraestrutura etc.) frequentemente
   **não publicam release nenhum** — só protocolam o ITR, muitas vezes em cima
   do prazo regulatório (D+45 do fim do trimestre).

---

## 1. A cadeia de divulgação

```
Empresa
  │  protocola via Empresas.NET (ENET) — mesmo sistema para tudo:
  │    • ITR / DFP  → documento ESTRUTURADO (dados contábeis campo a campo)
  │    • Release    → PDF não-estruturado (IPE, cat. "Dados Econômico-Financeiros")
  ▼
RAD / ENET (rad.cvm.gov.br)                      ← público em MINUTOS (D+0)
  │  consulta externa por documento; espelhado pela B3 (site de listadas)
  ▼
Portal Dados Abertos (dados.cvm.gov.br)          ← ZIPs anuais, atualização
     itr_cia_aberta_YYYY.zip  (estruturado)         SEMANAL do ano corrente
     dfp_cia_aberta_YYYY.zip  (estruturado)         → lag de 0 a ~7 dias
     ipe_cia_aberta_YYYY.zip  (índice dos PDFs)
```

Pontos-chave:

- O envio de **todas** as informações periódicas e eventuais é exclusivamente
  eletrônico, via Empresas.NET (acesso em `rad.cvm.gov.br/ENETWEB`). O ITR é
  documento eletrônico estruturado previsto no art. 22 da Resolução CVM 80/22.
- Documentos protocolados no ENET ficam **imediatamente disponíveis para
  consulta pública** no RAD (a exceção são pedidos explícitos de sigilo, que
  não se aplicam a ITR/DFP — esses não podem ser bloqueados).
- O dataset de dados abertos **não é a fonte primária** — é um índice/extração
  em lote regenerado periodicamente a partir do que já está no RAD.

## 2. Prazos regulatórios (Resolução CVM 80/2022)

| Documento | Prazo | Regra de antecipação |
|---|---|---|
| ITR | 45 dias do fim do trimestre (4º tri dispensado) | prática de mercado + orientação SEP: release não deve anteceder o formulário |
| DFs completas (anuais) | — | **devem ser entregues à CVM na mesma data em que forem colocadas à disposição do público** |
| DFP | 3 meses do fim do exercício **ou na mesma data do envio das DFs, o que ocorrer primeiro** | idem |

Ou seja: **para o resultado anual a simultaneidade é regra escrita** — a
empresa não pode publicar o PDF do resultado e mandar as demonstrações dias
depois. Para o ITR o prazo formal é D+45, mas o Ofício Circular Anual da
CVM/SEP orienta sobre divulgação antecipada de informações financeiras, e a
prática consolidada das companhias listadas é protocolar release e ITR na
mesma noite (a divulgação de resultado sem as demonstrações disponíveis expõe
a companhia a questionamento da SEP e risco de divulgação seletiva /
Resolução CVM 44).

## 3. Cadência real de cada rota de importação

| Rota | O que é | Latência após o protocolo | Formato / esforço |
|---|---|---|---|
| **RAD/ENET consulta externa** | busca por documento em `rad.cvm.gov.br` | **minutos** (D+0) | HTML/download por documento; existe "Download Múltiplo" documentado pela CVM (nota técnica própria) para baixar em lote |
| **Site da B3 (listadas)** | espelho dos mesmos documentos ENET | mesmo dia | scraping/endpoints não documentados |
| **Dados abertos — ITR/DFP** | `dados.cvm.gov.br/dados/CIA_ABERTA/DOC/{ITR,DFP}/DADOS/*.zip` — CSVs com DFs campo a campo (BPA, BPP, DRE, DFC...) + capa com **`DT_RECEB`** (data de protocolo) | **semanal** (ano corrente e A-1 "atualizados semanalmente com as eventuais reapresentações", conforme descrição oficial do dataset) → lag efetivo de 0 a ~7 dias | CSV pronto, zero parsing de PDF — rota mais simples |
| **Dados abertos — IPE** | `.../DOC/IPE/DADOS/ipe_cia_aberta_YYYY.zip` — índice de todos os PDFs (categoria, tipo, assunto, `Data_Entrega`, link de download) | semanal (mesma política) | o **link do PDF** dentro do CSV aponta para o RAD e funciona imediatamente; só o *índice* atrasa |

Duas conclusões práticas:

1. **Se a pergunta é "quando o CSV estruturado fica importável?"** — não é
   "dias depois porque a empresa demora a entregar"; é "até uma semana depois
   porque o portal de dados abertos regenera o ZIP semanalmente". A empresa
   entregou tudo no mesmo dia.
2. **Se o importador precisar de D+0**, a rota é RAD/ENET (ou B3), não o
   portal de dados abertos.

## 4. Padrão de comportamento das empresas

- **Listadas (Categoria A)** — padrão dominante: divulgação após fechamento do
  pregão (~18h–22h BRT), release (PDF) e ITR/DFP protocolados na mesma janela,
  tipicamente com minutos de diferença (a ordem varia: há empresas que
  protocolam o ITR primeiro e soltam o release em seguida, e vice-versa).
  Central de resultados própria + IPE + ITR são a mesma noite.
- **Emissores só-debêntures (Categoria B — comum na carteira deste monitor:
  SPEs de saneamento, transmissão, concessões)** — frequentemente **não há
  release em PDF**; o único evento é o protocolo do ITR/DFP, concentrado
  perto do prazo (D+45 tri / 3 meses anual). Para esses emissores a pergunta
  "PDF primeiro, dados depois?" nem se aplica: o dado estruturado É a
  divulgação.
- **Reapresentações**: ITR/DFP podem ser reapresentados (coluna `VERSAO` +
  novo `DT_RECEB` nos datasets). Um importador deve tratar o par
  (CNPJ, DT_REFER) como mutável e ficar com a maior versão.
- **Casos atípicos**: small caps que soltam release antes e protocolam o ITR
  no limite do prazo existem, mas são minoria e alvo de cobrança da SEP.

## 5. Recomendação para um futuro importador de fundamentos

| Requisito | Rota recomendada |
|---|---|
| Fundamentos com atualização "até 1 semana depois do resultado" (suficiente para monitorar alavancagem/spread de crédito) | **ZIPs de dados abertos ITR/DFP** — CSV pronto, estável, com `DT_RECEB` e `VERSAO`; probe semanal via `Last-Modified` do ZIP (mesmo padrão do probe ANBIMA/B3 já existente) |
| Reagir no dia do resultado | RAD/ENET Download Múltiplo ou consulta externa — mais frágil (ASP.NET, sem API pública garantida) |

O ZIP de dados abertos casa bem com a arquitetura atual do repo (probe HTTP →
cache check → import), bastando trocar a cadência de diária para semanal.

## 6. Validação empírica — script incluído

A política de rede desta sessão **bloqueia todos os hosts da CVM**
(`dados.cvm.gov.br`, `rad.cvm.gov.br`, `*.gov.br`), então a medição empírica
não pôde ser executada aqui. O script
[`scripts/estudo_cvm_timing.py`](../scripts/estudo_cvm_timing.py) faz a
medição quando rodado em ambiente com acesso (ex.: máquina local):

1. **Cadência do portal**: `HEAD` nos ZIPs de ITR/DFP/IPE do ano corrente e
   reporta `Last-Modified` (rodado diariamente por alguns dias, revela a
   cadência real de regeneração — a documentação diz semanal).
2. **Lag empresa-a-empresa**: baixa `ipe_cia_aberta_YYYY.zip` +
   `itr_cia_aberta_YYYY.zip`, casa release (categoria "Dados
   Econômico-Financeiros") com ITR (`DT_RECEB`, 1ª versão) por CNPJ ×
   trimestre e imprime a distribuição do delta em dias (mediana, % mesmo dia,
   % release antes, % ITR antes).

```bash
python3 scripts/estudo_cvm_timing.py --ano 2026            # estudo completo
python3 scripts/estudo_cvm_timing.py --ano 2026 --head-only # só cadência dos ZIPs
python3 scripts/estudo_cvm_timing.py --ano 2026 --empresa PETROBRAS --empresa AEGEA
```

## Fontes

- [Resolução CVM 80/2022 (texto consolidado, PDF)](https://conteudo.cvm.gov.br/export/sites/cvm/legislacao/resolucoes/anexos/001/resol080consolid.pdf) — prazos de ITR (45 dias), DFP (3 meses / mesma data das DFs) e entrega das DFs na data da colocação à disposição do público.
- [Dataset ITR — Portal Dados Abertos CVM](https://dados.cvm.gov.br/dataset/cia_aberta-doc-itr) e [Dataset DFP](https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp) — "arquivos referentes ao ano corrente e anterior serão atualizados semanalmente com as eventuais reapresentações".
- [Dataset IPE — Portal Dados Abertos CVM](https://dados.cvm.gov.br/dataset/cia_aberta-doc-ipe) — índice dos documentos não-estruturados (PDFs), mesma política de atualização semanal.
- [Envio de Informações — Sistema Empresas.NET (CVM)](https://www.gov.br/cvm/pt-br/assuntos/regulados/consultas-por-participante/companhias/envio-de-informacoes-enet) — envio exclusivamente eletrônico via ENET; acesso em rad.cvm.gov.br/ENETWEB.
- [Manual de Envio de Informações Periódicas e Eventuais (PDF)](https://conteudo.cvm.gov.br/export/sites/cvm/menu/regulados/companhias/Manual-Sistema-de-Envio-de-Informacoes-Periodicas-e-Eventuais.pdf) — disponibilidade pública dos documentos protocolados; ITR/DFP não podem ser bloqueados de consulta.
- [Nota Técnica — Download Múltiplo de Informações sobre Companhias (CVM)](https://conteudo.cvm.gov.br/menu/regulados/companhias/download_multiplo/manual_tecnico.html) — rota de download em lote direto do RAD.
- [Ofício Circular Anual 2026 CVM/SEP (PDF)](https://conteudo.cvm.gov.br/export/sites/cvm/legislacao/oficios-circulares/sep/anexos/oc-anual-sep-2026.pdf) — orientações sobre envio de informações periódicas e divulgação antecipada de informações financeiras.
- [Calendário CVM de entrega de informações (Resolução CVM 47)](https://www.gov.br/cvm/pt-br/assuntos/regulados/envio-de-informacoes-a-cvm-calendario).

**Limitações**: os fatos regulatórios e a cadência documentada foram
verificados via fontes oficiais (acima); a distribuição estatística do lag
release↔ITR por empresa e a cadência *real* dos ZIPs (a documentada é
semanal) precisam do script da seção 6 rodando com acesso à CVM.
