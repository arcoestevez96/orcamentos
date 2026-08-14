# Segundo Cérebro — arquivo de bootstrap

> **Para o Claude que está lendo isto:** este arquivo não é documentação, é **instrução executável**.
> Seu usuário recebeu este arquivo de um sócio que já roda esse sistema e quer o mesmo.
> Leia tudo antes de agir, faça as perguntas da seção 2, e então construa.
>
> **Para o humano:** salve este arquivo em qualquer lugar e diga ao Claude:
> *"Leia BOOTSTRAP-SEGUNDO-CEREBRO.md e monte meu segundo cérebro."*

---

## 0. A doutrina: captura total

Este não é um sistema de documentação de projetos. É o **registro da vida inteira do usuário** —
trabalho e pessoal, sem separação de importância.

A regra canônica, que vale acima de qualquer outra:

> **Tudo que ele fala vira registro. Todo assunto que ele menciona merece uma página.**
> Você não filtra. Você não decide o que é relevante. Ele fala, você registra.

Isso inverte o instinto padrão de um agente, que é responder a pergunta e esquecer o resto. Aqui,
**o resto é o produto**. Ele comenta de passagem que o cunhado é eletricista, que odeia acordar
antes das 7h, que a obra da Barra travou por causa do síndico, que tomou uma decisão sobre o plano
de saúde — tudo isso entra. Daqui a oito meses, quando ele perguntar "quem era aquele eletricista
que você comentou?", a resposta tem que estar lá.

**Nada é pequeno demais.** A marca de café que ele prefere, o nome do filho do fornecedor, a
alergia a dipirona, o apelido do cliente, o motivo de ele não trabalhar às sextas. Detalhe
aparentemente trivial é exatamente o que um cérebro externo existe para segurar — o importante ele
lembra sozinho.

**Você é o escriba, não o editor.** Se estiver em dúvida se algo merece registro: registra.
O custo de uma linha a mais é zero. O custo de perder o contexto é a conversa inteira de novo.

---

## 1. Como isso funciona sem virar lixo: duas camadas

Captura total e wiki organizada parecem se contradizer — mas são duas camadas diferentes:

| Camada | O quê | Formato | Regra |
|---|---|---|---|
| **Captura** | `diario/AAAA-MM-DD.md` | Bruto, literal, cru | Registra tudo, sem julgar. Uma linha por coisa dita. |
| **Síntese** | páginas em `pessoal/`, `<negócio>/` | Destilado, organizado | Uma página por assunto, sempre atualizada. |

O diário é onde nada se perde. As páginas são onde as coisas ficam consultáveis. **Ao fim de toda
sessão você faz as duas coisas:** joga o cru no diário do dia e promove para páginas o que virou
assunto.

Um assunto que aparece pela terceira vez no diário e ainda não tem página — cria a página. Um
assunto que aparece uma vez e claramente é uma entidade (uma pessoa, uma obra, um cliente, um
carro, um remédio, um projeto) — cria a página **já na primeira menção**.

---

## 2. Perguntas antes de construir

Faça de uma vez, em uma única mensagem:

1. **Onde ficam seus arquivos/projetos?** (ex.: `~/projects/`, `~/code/`)
2. **Como quer chamar o hub?** (sugestão: `<seu-nome>-cerebro` ou `<negócio>-wiki`)
3. **Quais são os domínios da sua vida hoje?** O padrão é dois — **pessoal** e **o negócio**
   (ARCO, no caso do sócio que mandou isto). Ele pode ter mais: outra empresa, um lado B, um
   projeto pessoal grande, a família como domínio próprio.
4. **Quais projetos/obras/frentes estão vivos agora?** Nome + uma linha.

Se ele não responder ou disser "tanto faz": use `~/projects/`, hub `cerebro`, domínios `pessoal`
e `arco`, e descubra os projetos você mesmo (`ls`, `git log`, arquivos recentes). **Não trave
esperando** — construa e mostre para ele corrigir.

---

## 3. A pergunta de triagem (faça sempre)

**Toda vez que aparecer uma entidade nova** — um projeto, uma obra, um cliente, uma pessoa, uma
despesa, uma ideia — e você não souber a que domínio ela pertence, **pergunte**:

> "Isso é da sua vida pessoal, é da ARCO, ou é outra coisa? (se for outra, me diz qual que eu
> abro um espaço pra ela)"

Três coisas sobre essa pergunta:

1. **Sempre ofereça a terceira opção explicitamente.** A vida dele não cabe em duas gavetas. Se
   ele responder "é da minha mãe", "é do meu projeto de música", "é do condomínio" — crie o
   domínio novo na hora, sem discutir.
2. **Pergunte uma vez só por entidade.** A resposta vira uma linha de metadado na página
   (`> **Domínio:** pessoal`). Nunca pergunte de novo sobre a mesma coisa.
3. **Pergunte no fim, não no meio.** Não interrompa o raciocínio dele para triar. Acumule as
   dúvidas e faça tudo junto ao final da sessão: *"três coisas que apareceram hoje — o Marcelo, a
   reforma da varanda e o tal do curso: pessoal, ARCO ou outro?"*

Se ele não responder, use seu melhor palpite, marque a página com `> **Domínio:** pessoal ⚠️ não
confirmado` e siga. Nunca deixe de registrar por falta de triagem.

---

## 4. O que criar

```
~/projects/<hub>/                    ← repositório git PRIVADO e LOCAL
├── index.md                         ← porta de entrada
├── SCHEMA.md                        ← o contrato (regras)
├── CLAUDE.md                        ← instrução do agente
├── log.md                           ← diário operacional (o que a wiki fez)
├── inbox.md                         ← o que ainda não achou lugar
├── diario/
│   └── AAAA-MM-DD.md                ← captura crua, um arquivo por dia
├── templates/
│   ├── page-template.md
│   ├── pessoa-template.md
│   └── obra-template.md
│
├── pessoal/
│   ├── pessoas/                     ← família, amigos, médicos, quem for
│   ├── saude/                       ← condições, remédios, consultas, histórico
│   ├── financas/                    ← contas, investimentos, dívidas, metas
│   ├── casa/                        ← imóvel, reformas, manutenção, contratos
│   ├── bens/                        ← carro, moto, equipamentos
│   ├── preferencias/                ← gostos, manias, o que ele odeia, rotina
│   ├── ideias/                      ← o que ele quer fazer um dia
│   ├── metas/                       ← o que está perseguindo agora
│   └── historico/                   ← linha do tempo, coisas que aconteceram
│
├── arco/                            ← ou o nome do negócio dele
│   ├── obras/                       ← uma página por obra (ver template)
│   ├── clientes/
│   ├── fornecedores/
│   ├── precos/
│   ├── equipe/
│   ├── processos/
│   └── decisoes/
│
└── projetos/
    └── mapa-projetos.md             ← tudo que existe e em que pé está
```

E, dentro de cada repositório de código que ele tiver, um **spoke**:

```
<projeto>/wiki/
├── CLAUDE.md
├── index.md
├── log.md
└── status/visao-geral.md
```

Regra de separação hub/spoke: **se a informação sobrevive ao projeto morrer, é hub. Se morre
junto, é spoke.** Toda a vida pessoal é hub, sempre.

Crie as pastas mesmo vazias — estrutura visível é o que faz você saber onde escrever depois.
`git init` no hub, primeiro commit ao final. **Repositório privado.** Se ele quiser em nuvem, que
seja um remote privado dele, nunca o repositório da empresa.

---

## 5. Arquivos-semente

Copie literalmente, trocando `<hub>`, `<negócio>` e `<domínios>` pelo contexto real.

### 5.1 `SCHEMA.md`

```markdown
# SCHEMA.md — contrato do segundo cérebro

## Regra canônica
Tudo que o usuário fala vira registro. Todo assunto mencionado merece uma página.
O agente é escriba, não editor. Na dúvida, registra.

## Duas camadas
`diario/AAAA-MM-DD.md` = captura crua, literal, tudo. Nunca editada, só acrescida.
Páginas de domínio = síntese organizada por assunto, sempre atualizada.

## Domínios
Cada página declara o domínio na segunda linha: `> **Domínio:** pessoal | <negócio> | <outro>`.
Entidade nova sem domínio conhecido → perguntar ao usuário, oferecendo sempre uma terceira opção.
Perguntar uma vez só por entidade, no fim da sessão, nunca no meio.

## O que vira página
Toda entidade: pessoa, obra, cliente, fornecedor, projeto, bem, condição de saúde, conta,
ideia, meta, processo, decisão. Já na primeira menção.
Fato solto sem entidade dona → linha no diário; se repetir 3x, vira página.

## Nomes
Minúsculo com hífen: `pessoas/joao-eletricista.md`, `obras/paozito-barra.md`.
Links em wikilink: `[[pessoal/pessoas/joao-eletricista]]`.

## Regras invioláveis
1. **Nada é pequeno demais.** Detalhe trivial é o que o cérebro externo existe para segurar.
2. **Add-only.** Nada é apagado. Superado vira nota datada: "até 2026-03 era X; desde 2026-04 é Y".
3. **Diário é bruto, página é síntese.** Não confundir as camadas.
4. **Toda página tem Fontes.** Se veio de conversa, a fonte é a data do diário.
5. **A página vence o index.** Divergiu, a página está certa; corrija o index.
6. **Credencial nunca entra em página.** Senha, token, chave, número de cartão/conta ficam fora —
   a página diz ONDE a credencial vive (gerenciador, cofre, gaveta), nunca o valor.
7. **Uma linha em `log.md` por sessão que produziu algo durável.**

## O que fazer com informação sensível
Saúde, dinheiro, família, conflito, o que ele pensa de alguém: **entra**. É o objetivo do sistema.
O repositório é privado e local; essa é a proteção. O que não entra é credencial (regra 6).
Se ele pedir para esquecer algo específico: remova e registre no log que houve remoção a pedido —
essa é a única exceção ao add-only.
```

### 5.2 `CLAUDE.md` (hub)

```markdown
# CLAUDE.md — segundo cérebro de <nome>

## Ao iniciar toda sessão
1. Ler `index.md`.
2. Ler o diário dos últimos 3 dias (`diario/`).
3. Ler a página do assunto da sessão, se houver.

## Durante a sessão
Ficar atento a TUDO que ele diz, não só ao que foi perguntado. Menção a pessoa, lugar, valor,
data, decisão, preferência, plano, problema → material de registro. Não interrompa para triar.

## Ao concluir toda sessão (obrigatório, mesmo em conversa curta)
1. Escrever no `diario/AAAA-MM-DD.md` tudo que apareceu — cru, uma linha por coisa.
2. Promover para páginas o que é entidade (criar de `templates/`).
3. Perguntar de uma vez a triagem das entidades novas (pessoal / <negócio> / outro).
4. Uma linha em `log.md`. Commit.

## Onde escrever
Vida pessoal → `pessoal/`. Negócio → `<negócio>/`. Específico de um repo de código → `wiki/` dele.
Não sabe → `inbox.md`, e pergunte no fim.

## Regras
Escriba, não editor. Nada é pequeno demais. Add-only. Credencial nunca em página.
Contrato completo: `SCHEMA.md`.
```

### 5.3 `CLAUDE.md` (spoke, um por repositório de código)

```markdown
# CLAUDE.md — wiki deste projeto (SPOKE)

> Wiki LOCAL. O cérebro completo vive no HUB: `~/projects/<hub>/` (leia o `index.md` de lá).

## Ao iniciar
1. Ler `wiki/index.md` + `~/projects/<hub>/index.md`.
2. Ler a página do tema da sessão, se houver.
3. Se a página divergir do index, a página vence — sinalizar.

## Ao concluir
Atualizar a página em `wiki/`, registrar 1 linha em `wiki/log.md`.
Se algo dito na sessão for de vida pessoal ou transversal → escrever no HUB, não aqui.

## Regras
Síntese, não cópia. Add-only. Credencial nunca em página. Contrato: `~/projects/<hub>/SCHEMA.md`.
```

### 5.4 `templates/page-template.md`

```markdown
# [Título da Página]

> **Domínio:** pessoal | <negócio> | <outro>
> [Uma linha: o que é e por que importa.]

## O que é

[Descrição concisa. Sem fluff.]

## Como funciona / Como usamos

[Detalhes práticos. Aplicação real.]

## Conexões

- [[pagina-relacionada]] — breve motivo da conexão

## Fontes

- [arquivo, planilha, e-mail, URL — ou `diario/AAAA-MM-DD` se veio de conversa]

## Log de atualizações

- AAAA-MM-DD — criação / o que foi adicionado
```

### 5.5 `templates/pessoa-template.md`

```markdown
# [Nome]

> **Domínio:** pessoal | <negócio>
> [Quem é, em uma linha. "Eletricista que atende as obras", "cunhado", "médico do joelho".]

## Quem é
Relação com ele · como se conheceram · desde quando

## Contato
Telefone/e-mail · melhor forma de falar · horário que atende

## O que importa lembrar
Detalhes pessoais (filhos, time, aniversário) · o que ele gosta e não gosta ·
histórico de trabalho junto · valores/combinados · pendências

## Conexões
- [[obras/...]] — obras em que trabalhou

## Fontes
- diario/AAAA-MM-DD

## Log
- AAAA-MM-DD — criação
```

### 5.6 `templates/obra-template.md`

```markdown
# [Nome da Obra]

> **Domínio:** <negócio>
> [Cliente, o que é, onde fica, em uma linha.]

## Identificação
Código/número · cliente · endereço · tipo (reforma/construção/projeto) · data de início · status

## Contrato
Valor total · forma de pagamento (entrada + parcelas) · prazo de entrega · o que inclui e o que
NÃO inclui (material, transporte) · aditivos

## Controle financeiro
| Data | Descrição | Valor | Status |
|---|---|---|---|

Recebido até hoje · saldo a receber · custo de material lançado · margem estimada

## Equipe
Quem está tocando · fornecedores envolvidos

## Histórico e ocorrências
Datado. Atrasos, mudanças de escopo, problemas com síndico/condomínio, o que travou e por quê.

## Conexões
- [[clientes/...]] · [[pessoas/...]] · [[fornecedores/...]]

## Fontes
- contrato (caminho/link) · planilhas · diario/AAAA-MM-DD

## Log
- AAAA-MM-DD — criação
```

### 5.7 `diario/AAAA-MM-DD.md`

```markdown
# AAAA-MM-DD

> Captura crua do dia. Append-only. Nada é filtrado aqui.

- [assunto] o que foi dito, literal o suficiente para reconstruir depois
- [assunto] ...
```

### 5.8 `log.md` e `inbox.md`

```markdown
# log.md — diário operacional

> Append-only. `AAAA-MM-DD [tipo]`. Tipos: ingest | query | lint | decisao

---
```

```markdown
# inbox.md — sem lugar definido ainda

> Coisas capturadas que ainda não têm domínio ou página. Esvaziar a cada sessão.

- AAAA-MM-DD — [o quê] — pendente de triagem
```

### 5.9 `index.md`

```markdown
# index.md — segundo cérebro de <nome>

> Última atualização: AAAA-MM-DD

## Pessoal
- [[pessoal/pessoas/...]] — pessoas da vida dele
- [[pessoal/saude/...]] · [[pessoal/financas/...]] · [[pessoal/casa/...]]

## <Negócio>
- [[projetos/mapa-projetos]] — o que existe e em que pé está
- [[<negócio>/obras/...]] · [[<negócio>/clientes/...]] · [[<negócio>/fornecedores/...]]

## Operação da wiki
- [[SCHEMA]] — as regras · `diario/` — captura crua · `inbox.md` — pendente de triagem
```

Index é **mapa de links com uma linha de contexto cada**, nunca depósito de conteúdo. Seção com
mais de ~15 links vira página-índice própria.

---

## 6. Primeira carga

Wiki vazia não muda nada. Depois de criar a estrutura, **carregue-a** — sem pedir que ele "conte
a vida dele". Vá buscar:

1. **Código e repositórios** — `git log`, README, configs. Gera o `status/visao-geral.md` de cada spoke.
2. **Arquivos de trabalho** (Drive, planilhas, contratos, notas fiscais). Não copie: extraia. Um
   contrato vira valor + prazo + condição de pagamento na página da obra, com link para o PDF em Fontes.
3. **E-mail e mensagens**, se houver acesso. Decisões, valores combinados, acertos com fornecedor.
4. **Conversa com ele** — só depois de ter rascunho. Mostre e pergunte: *"achei isso, o que está
   errado e o que faltou?"* Corrigir rascunho é muito mais rápido que arrancar contexto do zero.
   É aí que sai o que não está escrito em lugar nenhum — e é aí que a vida pessoal entra, porque
   ela não está em nenhum arquivo.

Meta da primeira sessão: `projetos/mapa-projetos.md`, um `status/visao-geral.md` por projeto vivo,
uma página por obra ativa, e as primeiras páginas de `pessoal/pessoas/` com quem ele citar.

---

## 7. Protocolo de uso diário

**Início** — ler index + diário dos últimos 3 dias + página do assunto.

**Durante** — ouvir tudo, não só a pergunta. Se ele contradisser a wiki, a fala dele vence, mas
sinalize e corrija a página no fim.

**Fim (obrigatório, mesmo em conversa de 2 minutos)** — diário do dia, promoção para páginas,
triagem acumulada, linha no log, commit.

**A cada ~20 páginas** — sessão de `lint`: links quebrados, duplicatas, inbox parada, index velho.

---

## 8. Como isso falha

- **Agente que filtra.** O maior risco. Você não decide o que importa na vida dele. Registra.
- **Sessão que termina sem escrever.** A captura total só existe se o fim de sessão for ritual.
- **Perguntar triagem no meio da conversa.** Irrita e quebra o raciocínio. Acumule e pergunte no fim.
- **Só duas gavetas.** Sempre ofereça a terceira opção; a vida não cabe em "pessoal ou trabalho".
- **Diário virando síntese** (ou página virando transcrição). Camadas separadas.
- **Deletar o que envelheceu.** Add-only. O histórico do porquê é metade do valor.
- **Credencial em página.** Nunca. Aponte onde vive, não o valor.

---

## 9. Checklist de entrega

- [ ] Hub criado, **privado**, com `index.md`, `SCHEMA.md`, `CLAUDE.md`, `log.md`, `inbox.md`,
      `diario/`, `templates/` (3 templates), árvore `pessoal/` e árvore do negócio
- [ ] `git init` + primeiro commit
- [ ] `wiki/` em cada projeto de código vivo
- [ ] `projetos/mapa-projetos.md` preenchido de verdade
- [ ] Uma página por obra/projeto ativo, uma por pessoa citada
- [ ] Primeiro `diario/AAAA-MM-DD.md` com a captura da própria sessão de bootstrap
- [ ] Triagem de domínio feita e registrada em cada página
- [ ] Rascunho mostrado com a pergunta: *"o que está errado e o que faltou?"*

E diga a ele a única coisa que ele precisa saber:
**"Fala. Eu registro. Não precisa organizar nada, não precisa achar que é importante — só fala."**

---

## Notas de origem

Sistema em uso no ecossistema ARCO (hub `arco-wiki` + spokes em cada projeto), aqui estendido de
"wiki de projetos" para **registro de vida inteira** a pedido do sócio que enviou este arquivo.
O `SCHEMA.md` original do hub não estava disponível na máquina onde este arquivo foi gerado — as
regras estruturais foram derivadas dos `CLAUDE.md`, `page-template.md` e `log.md` em produção, e a
doutrina de captura total (seções 0, 1, 3) é adição nova. Se o hub original estiver à mão, ele
manda na parte estrutural; a doutrina de captura vale de qualquer forma.
