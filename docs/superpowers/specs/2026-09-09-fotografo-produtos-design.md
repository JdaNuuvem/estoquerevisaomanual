# Fotógrafo de produtos — Design

## Contexto

A auditoria de estoque (bipagem + Contagem/Fase 2) já roda por loja. Depois
de bipada a existência de um produto, alguém precisa fotografar cada item
fisicamente — hoje isso não tem ferramenta nenhuma no sistema.

Este documento desenha um novo papel de usuário, **fotógrafo**, com uma
tela dedicada e gamificada: ele entra, o app mostra um produto por vez (na
ordem em que foi bipado), ele tira a foto com a câmera do navegador
(preview ao vivo, não abrir o app de câmera do celular) e envia — o app
avança sozinho pro próximo. Também pode pular a fila lendo o código de
barras de um produto específico já bipado, fora de ordem.

Não existe hoje, no projeto: conceito de papel de usuário (`users.json` só
tem nome/email/senha/loja, sem distinção bipador/outro), nem qualquer
armazenamento de arquivo binário (só JSON no volume Docker persistente).
Ambos precisam ser criados.

## Papéis e login

`users.json` ganha o campo `role` (`"bipador"` | `"fotografo"`, ausente ou
`null` = `bipador`, pra não quebrar os cadastros existentes). O formulário
"Novo Bipador" do admin ganha um seletor de papel (rótulo passa a ser
"Novo Usuário"). Login (`POST /api/auth/login`) não muda — já retorna o
objeto do usuário completo (menos o hash), então `role` chega pro
frontend automaticamente.

No frontend, depois do login, `S.user.role === 'fotografo'` desvia direto
pra tela de Fotografia (sem acesso a Bipagem/Relatórios/Contagem — telas
distintas de papéis diferentes). `role === 'bipador'` (ou ausente) segue o
fluxo atual, inalterado.

## Fila de produtos

Fonte: a mesma sessão mesclada de auditoria já usada em
`getMergedSessionForFilial` (todas as sessões da loja do fotógrafo,
`encontrados` combinado em ordem cronológica de bipagem). A fila do
fotógrafo é essa lista **menos** os produtos que já têm foto registrada
(ver `fotos_por_filial.json` abaixo), na mesma ordem de bipagem.

"Pular" um produto é só uma reordenação **local, na tela** (não persiste
no servidor): o item pulado volta pro fim da fila carregada naquela
sessão do navegador. Se a página recarregar, a fila é recalculada do
zero (ordem de bipagem, sem foto) — o pulado volta pra posição original,
o que é aceitável (não trava nada, só perde a fila "ele foi pro fim"
momentânea).

Fim da fila: quando não sobra nenhum produto sem foto, mostra uma tela de
"loja concluída" (com o total fotografado), sem travar — se novos
produtos forem bipados depois, reaparecem na fila na próxima vez que o
fotógrafo abrir a tela.

## Armazenamento de fotos

Novo arquivo `fotos_por_filial.json`:

```json
{
  "63": {
    "123": {
      "arquivo": "63/123.jpg",
      "fotografadoPor": "fotografo@status.com",
      "fotografadoEm": "2026-09-09T14:30:00"
    }
  }
}
```

Chave externa = `filialId` (string), interna = `produtoId` (string).
Arquivos ficam em `DATA_DIR/fotos/<filialId>/<produtoId>.jpg` — mesmo
volume Docker persistente que já guarda `audit_sessions.json`,
`sales_cache.json` etc, sem storage externo novo. Reenviar uma foto do
mesmo produto **sobrescreve** o arquivo e atualiza `fotografadoEm` (não
guarda histórico de versões — não foi pedido).

O navegador redimensiona/comprime a foto antes de enviar (captura o frame
do `<video>` num `<canvas>`, exporta como JPEG ~85% de qualidade, teto de
1600px no lado maior) — evita fotos de 10+ MB de celular lotando o volume
e deixando o upload lento em rede de loja.

## Endpoints novos (server.py)

- `GET /api/fotografo/fila?filialId=X` — retorna a lista de produtos
  bipados na loja **sem foto ainda**, na ordem de bipagem, já mesclando
  com o catálogo (mesmo padrão do endpoint externo de produtos
  existentes). Também retorna `total_bipado` e `total_fotografado` pra
  montar a barra de progresso.
- `POST /api/fotografo/foto` — multipart, campos `filialId`, `produtoId`,
  `foto` (arquivo). Salva em disco, atualiza
  `fotos_por_filial.json`. Sobrescreve se já existir.
- `GET /api/fotografo/fotos?filialId=X` — lista os produtos já
  fotografados daquela loja (pra aba "Já fotografados" do fotógrafo e pra
  galeria do admin), com a URL de cada miniatura.
- `GET /api/fotos/<filialId>/<produtoId>.jpg` — serve o arquivo (checagem
  simples de path traversal via `secure_filename`/validação de que
  `produtoId` é inteiro).
- `GET /api/admin/fotos/zip?filialId=X&adminPassword=...` — empacota todas
  as fotos daquela loja num `.zip` gerado na hora (`zipfile` da stdlib,
  sem dependência nova) e devolve pra download.

## Fluxo de captura (tela do fotógrafo)

Uma tela cheia, um produto por vez:

1. Topo: descrição/EAN/SKU do produto atual + barra de progresso
   (`fotografados / total_bipado` da loja).
2. Preview de câmera ao vivo (câmera traseira por padrão,
   `getUserMedia`/`facingMode: environment`) — reaproveita o mesmo padrão
   de modal de câmera já usado no scanner de código de barras
   (`#camera-modal`), adaptado pra capturar foto em vez de decodificar
   barcode.
3. Botão grande "Capturar" — tira o frame atual, mostra em tela cheia com
   dois botões: "Repetir" (volta pro preview ao vivo) e "Enviar" (sobe a
   foto via `POST /api/fotografo/foto`, avança automaticamente pro
   próximo produto da fila).
4. Botão secundário "Pular", sempre visível — manda o produto atual pro
   fim da fila local (ver seção Fila acima) e mostra o próximo.
5. Botão "Ler código de barras" — abre o mesmo scanner da bipagem
   (`findByEanOuCodigo`, aceita EAN ou código interno). Se o código lido
   corresponde a um produto **já bipado nessa loja** (está na sessão
   mesclada), pula direto pra ele fora de ordem — tira a foto normalmente
   e, ao enviar, volta pra fila na posição em que estava antes de usar o
   leitor. Se o código não corresponde a nenhum produto bipado, mostra a
   mesma mensagem de erro "não encontrado" já usada na bipagem.
6. Aba separada "Já fotografados" (lista com miniatura, usa
   `GET /api/fotografo/fotos`) — clicar num item reabre a câmera pra
   aquele produto especificamente, sobrescrevendo a foto ao enviar.

## Visão do admin

Nova sub-seção (dentro de Bipadores, ou aba própria) com, por loja:

- Progresso: `X de Y produtos fotografados` + barra.
- Galeria: miniatura clicável por produto (abre a foto em tamanho real).
- Botão **"Baixar tudo (.zip)"** por loja, chamando
  `GET /api/admin/fotos/zip?filialId=X`.

Cadastro de fotógrafo entra no mesmo formulário "Novo Bipador" existente,
que passa a ter o seletor de papel descrito acima.

## Fora de escopo (YAGNI)

- Mais de 1 foto por produto (galeria de ângulos) — v1 é 1 foto,
  substituível.
- Gamificação além da barra de progresso (streak, som, ranking entre
  fotógrafos) — pode entrar depois se fizer falta.
- Histórico de versões de foto (quem trocou, quando, foto anterior) — só
  a foto atual é guardada.
- Fotografar produto que nunca foi bipado na loja — leitor avulso só
  aceita produtos já presentes na sessão de auditoria daquela loja.
- Fila persistida no servidor (ordem "pulei este") — pular é só reordenar
  a tela local, não grava estado de pulo no backend.
