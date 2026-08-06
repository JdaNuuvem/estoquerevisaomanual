# Fluxo de aprovação de entrada de estoque — Design

## Contexto

Hoje o projeto tem só dois papéis: `admin` (senha única global) e `bipador`
(nome/email/senha/filialId, sem distinção de função — usado só pra contagem/
auditoria de catálogo). O usuário opera 4 setores reais: Estoque (estoquista),
Balcão da Loja, Gerente da Loja e Gerente Geral do Estoque — nenhum modelado
hoje.

**Natureza do sistema**: isso não substitui o i9logic. O i9logic continua
sendo o ERP real, rodando nos pontos de venda (PDV) de cada loja, e continua
sendo a fonte de verdade para catálogo, preços e vendas. Este projeto é uma
**camada de controle e rastreabilidade por cima do i9logic** — uma "muleta
organizatória" para saber quanto cada loja tem em estoque e para onde os
produtos estão indo, com um fluxo de aprovação que o i9logic não oferece.

**Confirmado por investigação direta na API i9logic (`api.i9logic.net`)**: a
API é somente leitura, com um único PATCH pontual (`pedidos_produtos/{id}`,
campo `datamontagem`). Não existe endpoint de criação de pedidos/movimentações.
O evento `TRANSFERENCIA ENTRE FILIAIS` (id 2, direção `T`) existe cadastrado
no ERP, mas só pode ser lançado por quem tem acesso à tela do i9logic — nosso
sistema não pode criar isso via API. Isso confirma que o fluxo de aprovação
de entrada **precisa existir neste projeto**, com um saldo próprio como fonte
de verdade operacional — não há como delegar a criação ao i9logic.

## Objetivo desta fase

Fluxo de aprovação de **entrada de mercadoria** em duas etapas, e um saldo de
estoque local por loja/produto que essa entrada alimenta e que as vendas do
i9logic debitam automaticamente.

**Fora de escopo desta fase**: transferência entre lojas (o usuário quer
decidir a regra de aprovação disso depois — não bloqueia esta spec) e saída
manual (perda, quebra, devolução — não mencionada como necessária agora,
YAGNI). Saída = venda no PDV, sempre lida do i9logic, nunca manual.

## Papéis e permissões

| Papel | Solicita entrada | Aprova nível 1 (loja) | Aprova nível 2 (final) | Vê |
|---|---|---|---|---|
| Estoquista | Sim | — | — | Próprias solicitações + saldo da própria loja |
| Balcão da Loja | Sim | — | — | Próprias solicitações + saldo da própria loja |
| Gerente da Loja | — | Sim (só da própria loja) | — | Tudo da própria loja (solicitações, saldo, histórico) |
| Gerente Geral do Estoque | — | — | Sim (todas as lojas) | Tudo, todas as lojas |

`users.json` ganha um campo `role` (`estoquista` | `balcao` | `gerente_loja` |
`gerente_geral`), preenchido pelo admin ao cadastrar o usuário (mesmo fluxo
de `POST /api/admin/bipadores` já existente, só adicionando o campo). Um
usuário com `role: gerente_loja` ou `estoquista`/`balcao` continua tendo
`filialId` (a loja dele); `gerente_geral` não é vinculado a uma loja
específica — vê todas.

## Fluxo de aprovação (2 níveis)

```
Estoquista ou Balcão da Loja cria solicitação de entrada
  (produto, quantidade, valor unitário)
        │
        ▼
status: pendente_gerente_loja
        │
        ▼
Gerente da Loja (mesma loja do solicitante) decide
   │                                    │
 rejeita                              aprova
   │                                    │
   ▼                                    ▼
status: rejeitado_gerente_loja   status: pendente_gerente_geral
   (fim, registrado)                    │
                                         ▼
                          Gerente Geral do Estoque decide
                             │                        │
                           rejeita                   aprova
                             │                        │
                             ▼                        ▼
                  status: rejeitado_gerente_geral   status: aprovado
                     (fim, registrado)               │
                                                       ▼
                                          EFETIVA: soma quantidade
                                          no saldo_local da loja/produto
```

- Rejeição em qualquer nível é **definitiva** — a solicitação fica registrada
  com esse status para sempre. Se precisar corrigir, o solicitante cria uma
  solicitação nova (mais simples que editar/reenviar; histórico fica limpo:
  cada solicitação é imutável uma vez decidida).
- Cada solicitante só vê as próprias solicitações. Gerente da Loja só vê a
  fila (`pendente_gerente_loja`) e o histórico da própria loja. Gerente Geral
  vê a fila (`pendente_gerente_geral`, já aprovadas no nível 1) e o histórico
  de todas as lojas.

## Modelo de dados

**`movimentacoes.json`** (dict, chave = id da movimentação):
```json
{
  "mov_1785900000000": {
    "tipo": "entrada",
    "produtoId": 123, "descricao": "...", "ean": "...", "codproduto": "...",
    "filialId": 63, "filialNome": "CHARME COSMETICOS",
    "quantidade": 50, "valorUnitario": 12.90,
    "solicitante": {"email": "...", "nome": "...", "role": "estoquista"},
    "status": "pendente_gerente_loja",
    "aprovacaoLoja": null,
    "aprovacaoGeral": null,
    "criadoEm": "2026-08-06T11:00:00"
  }
}
```
`aprovacaoLoja`/`aprovacaoGeral`, quando decididos, ficam
`{"por": "email", "nome": "...", "decisao": "aprovado"|"rejeitado", "em": "..."}`.
`tipo` já existe como campo pensando na fase futura de transferência (que
usará `"transferencia"` e campos adicionais `filialOrigemId`/`filialDestinoId`
— não implementados agora).

**`estoque_local.json`** (dict, chave = `"<filialId>_<produtoId>"`, mesmo
padrão de chave composta que `stockMap` já usa no frontend):
```json
{ "63_123": { "saldo": 47, "atualizadoEm": "2026-08-06T11:05:00" } }
```
Começa **zerado** — sem carga inicial a partir de contagem de auditoria ou
do i9logic. Só reflete o que passar pelo fluxo de aprovação e pelas vendas
sincronizadas a partir do momento em que o sistema entrar em uso (decisão
explícita do usuário: simplicidade sobre precisão retroativa).

**`vendas_sync_progress.json`**: controla até onde a sincronização de vendas
já avançou (última página/data processada), para a rotina seguinte buscar só
pedidos novos em vez de reprocessar o histórico inteiro a cada execução.

## Sincronização de vendas (débito automático)

Rotina periódica (mesmo padrão de thread em background já usado no projeto
para `_run_last_sale_scan`/`_run_vendas_2026_full`) que:

1. Busca pedidos novos via `GET /pedidos?data_de=<última execução>&data_ate=hoje`
   (a API exige um filtro mínimo — `data` é um dos aceitos, já usado em
   outras rotinas deste projeto).
2. Para cada pedido novo, busca os itens via `pedidos_produtos?idpedido=X`.
3. Para cada item, debita `quantidade` do `estoque_local["<filialId>_<produtoId>"]`,
   usando o campo `filial_estoque` do pedido como a loja a debitar (é o
   campo que a doc da API descreve como "filial de estoque" — mais direto
   para "de onde saiu o produto" do que `filial_venda`, que é comercial e
   pode divergir em casos de venda cruzada entre lojas).
4. Salva o progresso (`vendas_sync_progress.json`) para a próxima execução
   continuar de onde parou.

Roda a cada N minutos (ex.: 15) via `threading.Timer`/loop simples, mesmo
padrão de "rotina em background disparada e monitorada por status" já
estabelecido no projeto (`/api/admin/.../start` + `/api/.../status`).

## Telas (visão por papel, alto nível — detalhamento fica para o plano)

- **Estoquista / Balcão da Loja**: formulário "Nova entrada" (produto —
  busca por EAN/nome como já existe na auditoria —, quantidade, valor
  unitário) + lista das próprias solicitações com status.
- **Gerente da Loja**: fila de solicitações pendentes da própria loja
  (aprovar/rejeitar) + histórico + saldo local dos produtos da loja.
- **Gerente Geral do Estoque**: fila de solicitações pendentes (já aprovadas
  no nível 1, de todas as lojas) + histórico completo + saldo local de todas
  as lojas.

## Fora de escopo (YAGNI)

- Transferência entre lojas — regra de aprovação a decidir numa fase
  seguinte; o modelo de dados já reserva o campo `tipo` para isso.
- Saída manual (perda, quebra, devolução ao fornecedor) — não pedida agora;
  saída é sempre venda, sempre via sincronização do i9logic.
- Carga inicial do saldo a partir de contagem de auditoria ou do i9logic —
  saldo começa zerado por decisão explícita do usuário.
- Edição/reenvio de solicitação rejeitada — rejeição é definitiva, corrige
  criando uma solicitação nova.
- Notificação em tempo real (push/e-mail) para os aprovadores — por ora, o
  aprovador precisa entrar no sistema e ver a fila.
