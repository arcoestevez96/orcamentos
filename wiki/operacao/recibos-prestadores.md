# Recibos de Prestadores de Serviço

> Modelo e dados fixos usados para emitir recibos de serviços prestados na operação ARCO/obras.

## O que é

Recibo simples (1 página, A4) emitido pelo **prestador** declarando ter recebido determinada quantia
do **tomador/contratante**, com tabela de itens (quant. / descrição / valor unit. / total), valor total,
bloco de dados de pagamento (PIX), assinatura do prestador e local/data.

Não é nota fiscal — é comprovante de recebimento. Quando o tomador exige NFS-e, a Inscrição Municipal
dele entra como dado de faturamento (ver abaixo).

## Como funciona / Como usamos

**Papéis (não confundir):**
- **Prestador** = quem executa e RECEBE o valor → aparece em "declarar que ... recebeu de".
- **Tomador** = quem contrata e PAGA → aparece após "recebeu de", com CNPJ, Inscrição Municipal e endereço.

**Prestador recorrente — Washington da Silva**
- CNPJ: 60.280.175/0001-21 — base em Barra do Pojuca - BA.
- Serviços típicos: montagem/desmontagem de móveis, deslocamento (combustível reembolsado como item).
- Pagamento por **PIX, chave = CPF do Washington** (chave em si não fica registrada na wiki; está no
  recibo entregue e nos contatos). O bloco "Dados para pagamento - PIX" traz chave + favorecido.

**Tomador — Horto Sapucaia Empreendimento Imobiliário Ltda**
- CNPJ: 43.246.850/0001-87 · Inscrição Municipal: 00.826.980/001-04.
- Endereço de entrega: Apartamento 504 (Decorado), R. da Sapucaia, Lotes 33 e 34 — Horto Florestal,
  Salvador - BA, 40295-110.
- Cliente do apartamento decorado; sempre pedir os 3 dados (CNPJ + IM + endereço de entrega) porque
  a IM é exigida no faturamento municipal de Salvador.

**Recibos emitidos**
- Nº 231 — 12/09/2026 — R$ 350,00 — Washington da Silva → Horto Sapucaia.
  Itens: combustível/gasolina R$ 150,00 + desmontagem e montagem do sofá R$ 200,00.
  Nota datada: a 1ª versão do recibo trazia "Gabriel Ribeiro" como pagador; corrigida para
  Horto Sapucaia, que é quem contratou e paga o serviço do Washington.

**Como gerar:** o PDF é montado por script (reportlab, A4) — texto declaratório justificado, tabela de
itens com grade, bloco PIX em caixa e assinatura centralizada. Só mudam: nº, valor, data, itens e as
partes.

## Conexões

- [[status/visao-geral]] — projeto ABRIU (orçamentos); este recibo é da operação, não do produto

## Fontes

- Recibo Nº 231 (PDF original enviado pelo usuário) + dados de faturamento passados pelo cliente
- Sessão de 2026-09-15

## Log de atualizações

- 2026-09-15 — criação: papéis prestador/tomador, dados do Washington da Silva e da Horto Sapucaia,
  registro do recibo Nº 231 e do pagamento via PIX.
