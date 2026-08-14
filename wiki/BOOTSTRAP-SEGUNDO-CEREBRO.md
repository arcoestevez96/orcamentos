# Segundo Cérebro — arquivo de bootstrap

> **Para o Claude que está lendo isto:** este arquivo não é documentação, é **instrução executável**.
> Seu usuário recebeu este arquivo de um sócio que já roda esse sistema e quer o mesmo.
> Leia tudo antes de agir, faça as 3 perguntas da seção 1, e então construa.
>
> **Para o humano:** salve este arquivo em qualquer lugar e diga ao Claude:
> *"Leia BOOTSTRAP-SEGUNDO-CEREBRO.md e monte meu segundo cérebro."*

---

## 0. O que é esse sistema

Uma wiki em markdown puro, versionada em git, escrita **pelo agente** e lida **pelo agente** no
início de cada sessão. O objetivo não é documentar: é fazer o Claude começar toda conversa já
sabendo o que ficou decidido nas anteriores, sem o humano precisar recontar.

Arquitetura **hub-and-spoke**:

- **HUB** — um repositório só de conhecimento, fora dos projetos. Guarda o que vale para vários
  projetos ao mesmo tempo: clientes, fornecedores, preços, processos, decisões de negócio, mapa
  dos projetos.
- **SPOKE** — uma pasta `wiki/` dentro de cada projeto/repositório. Guarda o que só vale ali:
  arquitetura, status, decisões técnicas locais.

Regra de ouro da separação: **se a informação sobrevive ao projeto morrer, é HUB. Se morre junto,
é SPOKE.**

O que faz funcionar não é a wiki — é o `CLAUDE.md` que obriga o agente a ler no início e escrever
no fim. Sem isso vira pasta de arquivo morto.

---

## 1. Perguntas antes de construir

Faça as três de uma vez, em uma única mensagem, e espere a resposta:

1. **Onde ficam seus projetos?** (ex.: `~/projects/`, `~/code/`, `~/Documents/trabalho/`)
2. **Como quer chamar o hub?** (sugestão: `<seu-negócio>-wiki`, ex.: `arco-wiki`, `joao-wiki`)
3. **Quais são os 3–6 projetos/frentes vivos hoje?** Nome + uma linha do que é.

Se o usuário não responder ou disser "tanto faz", use os padrões: `~/projects/`, hub chamado
`wiki`, e descubra os projetos você mesmo (`ls ~/projects`, `git log` de cada um). **Não trave
esperando resposta** — construa com o padrão e mostre o resultado para ele corrigir.

---

## 2. O que criar

```
~/projects/<hub>/                 ← repositório git próprio
├── index.md                      ← porta de entrada do hub
├── SCHEMA.md                     ← o contrato (regras deste sistema)
├── log.md                        ← diário append-only do hub
├── CLAUDE.md                     ← instrução do agente no hub
├── templates/
│   └── page-template.md
├── projetos/
│   └── mapa-projetos.md          ← primeira página real: o que existe e em que pé está
├── clientes/
├── fornecedores/
├── processos/
└── decisoes/

<cada projeto>/wiki/              ← dentro do repo do projeto
├── CLAUDE.md
├── index.md
├── log.md
└── status/
    └── visao-geral.md
```

Crie as pastas vazias mesmo sem páginas — a estrutura visível é o que faz o agente saber onde
escrever depois. `git init` no hub e primeiro commit ao final.

---

## 3. Arquivos-semente

Copie o conteúdo abaixo **literalmente**, trocando só `<hub>` pelo nome escolhido e `<negócio>`
pelo contexto do usuário.

### 3.1 `~/projects/<hub>/SCHEMA.md`

```markdown
# SCHEMA.md — contrato da wiki

## Estrutura
Hub (`~/projects/<hub>/`) = conhecimento transversal. Spoke (`<projeto>/wiki/`) = conhecimento local.
Se sobrevive ao projeto morrer → hub. Se morre junto → spoke.

## Anatomia de uma página
Título · uma linha de resumo · O que é · Como funciona/Como usamos · Conexões · Fontes · Log.
Ver `templates/page-template.md`.

## Nomes
Arquivos em minúsculo com hífen: `cliente-divino-paozito.md`, `preco-mao-de-obra.md`.
Links internos em wikilink: `[[clientes/cliente-divino-paozito]]`.

## O que vira página
Vira página o que vai ser consultado de novo: cliente, fornecedor, preço, processo, decisão
com consequência, status de projeto. NÃO vira página: evento isolado, conversa, tarefa concluída
sem regra nova — isso é uma linha no `log.md`.

## Regras invioláveis
1. **Síntese, não cópia.** Página é destilado. Documento bruto fica na fonte; a página aponta pra ela.
2. **Add-only.** Nada é apagado. Informação superada vira nota datada: "até 2026-03 era X; desde
   2026-04 é Y (motivo)."
3. **Segredo nunca entra em página.** Nem senha, token, chave, dado bancário. Aponte onde está.
4. **Toda página tem Fontes.** Sem fonte rastreável, é boato — marque `⚠️ não confirmado`.
5. **A página vence o index.** Se divergirem, a página está certa e o index está velho — corrija o index.
6. **Uma linha de log por sessão que produziu algo durável.**
```

### 3.2 `~/projects/<hub>/CLAUDE.md`

```markdown
# CLAUDE.md — wiki de <negócio> (HUB)

## Ao iniciar
Ler `index.md` e a página do tema da sessão. Em dúvida sobre as regras, ler `SCHEMA.md`.

## Ao concluir algo durável
Atualizar/criar a página correspondente (a partir de `templates/page-template.md`) e registrar
1 linha em `log.md`.

## Onde escrever
Transversal (cliente, fornecedor, preço, processo, decisão de negócio) → aqui.
Específico de um projeto → no `wiki/` daquele projeto.

## Regras
Síntese, não cópia. Add-only. Segredos nunca em página. Contrato completo: `SCHEMA.md`.
```

### 3.3 `<projeto>/wiki/CLAUDE.md` (um por projeto)

```markdown
# CLAUDE.md — wiki deste projeto (SPOKE)

> Wiki LOCAL. O conhecimento compartilhado vive no HUB: `~/projects/<hub>/` (leia o `index.md` de lá).

## Ao iniciar
1. Ler `wiki/index.md` (este projeto) + `~/projects/<hub>/index.md` (hub).
2. Ler a página do tema da sessão, se houver.
3. Se a página divergir do index, a página vence — sinalizar.

## Ao concluir algo durável
Atualizar a página em `wiki/` (criar de `~/projects/<hub>/templates/page-template.md` se não
existir) e registrar 1 linha em `wiki/log.md`.

## Onde escrever
Arquitetura, status e decisões locais → aqui. Vale para vários projetos → no HUB.

## Regras
Síntese, não cópia. Add-only. Segredos nunca em página. Contrato completo: `~/projects/<hub>/SCHEMA.md`.
```

### 3.4 `templates/page-template.md`

```markdown
# [Título da Página]

> [Uma linha: o que é e por que importa.]

## O que é

[Descrição concisa. Sem fluff.]

## Como funciona / Como usamos

[Detalhes práticos. Foco na aplicação real.]

## Conexões

- [[pagina-relacionada-1]] — breve motivo da conexão
- [[pagina-relacionada-2]] — breve motivo da conexão

## Fontes

- [arquivo bruto, planilha, e-mail ou URL de origem]

## Log de atualizações

- AAAA-MM-DD — criação / o que foi adicionado
```

### 3.5 `log.md` (hub e cada spoke)

```markdown
# log.md — diário de <nome>

> Append-only. `AAAA-MM-DD [tipo]`. Tipos: ingest | query | lint | decisao

---
```

Os quatro tipos: **ingest** (entrou conhecimento novo de uma fonte), **query** (pergunta
respondida que valeu registrar), **lint** (arrumação/correção da própria wiki), **decisao**
(algo foi decidido e tem consequência).

### 3.6 `index.md` (hub)

```markdown
# index.md — Wiki de <negócio>

> Hub do ecossistema. Última atualização: AAAA-MM-DD

## Projetos
- [[projetos/mapa-projetos]] — o que existe, em que pé está, quem toca

## Clientes
<!-- adicionar conforme surgirem -->

## Fornecedores
<!-- -->

## Processos
<!-- -->

## Decisões
<!-- -->
```

O index é **um mapa de links com uma linha de contexto cada**, nunca um depósito de conteúdo.
Se uma seção passar de ~15 links, ela virou assunto próprio: crie uma página-índice para ela.

---

## 4. Primeira carga (o passo que a maioria pula)

Uma wiki vazia não muda nada. Depois de criar a estrutura, **carregue-a** — sem pedir ao usuário
que "conte tudo". Vá buscar:

1. **Código e repositórios.** Para cada projeto: `git log --oneline -30`, README, arquivos de
   config, estrutura de pastas. Isso já dá `status/visao-geral.md` de cada spoke: o que é, stack,
   como roda, onde está publicado, o que está pendente.
2. **Arquivos de trabalho** (Drive, pastas locais, planilhas, contratos). Não copie: extraia as
   regras. Um contrato vira preço + prazo + condição de pagamento numa página de cliente, com
   link para o PDF original em Fontes.
3. **E-mail e mensagens**, se houver acesso. Procure por decisões, valores acordados, combinados
   com fornecedor.
4. **O próprio humano.** Só depois de ter um rascunho, mostre e pergunte: *"achei isso, o que
   está errado e o que faltou?"* — corrigir um rascunho é infinitamente mais rápido do que
   escrever do zero, e é aí que sai o conhecimento que não está em lugar nenhum.

Meta da primeira sessão: **`projetos/mapa-projetos.md` + um `status/visao-geral.md` por projeto
vivo.** Isso já paga o custo. O resto cresce por uso.

Ao terminar cada carga, registre no `log.md` do lugar certo: `AAAA-MM-DD [ingest]` + fonte +
páginas criadas.

---

## 5. Protocolo de uso diário

**Início de sessão** — ler o `index.md` do lugar onde está trabalhando + a página do tema. Se a
sessão for num projeto, ler os dois index (spoke e hub).

**Durante** — quando o usuário afirmar algo que contradiz a wiki, a fala dele vence, mas
**sinalize a divergência** e corrija a página no fim.

**Fim de sessão** — pergunte-se: *"o que eu sei agora que não estava escrito?"* Se a resposta
tem consequência futura, vira página ou nota datada numa página existente. Sempre uma linha no
log. Commit.

**A cada ~20 páginas** — sessão de `lint`: links quebrados, duplicatas, páginas que viraram
índice inchado, seções mortas do index.

---

## 6. Como isso falha (evite)

- **Wiki que só o humano escreve.** Escrever é trabalho do agente; o humano corrige.
- **Página que é cópia colada.** Se dá para ler o original em 30s, não precisa de página —
  precisa de link.
- **Deletar o que ficou velho.** Add-only, sempre. O histórico do porquê é metade do valor.
- **Index virando conteúdo.** Index é mapa. Conteúdo é página.
- **Ler no início e não escrever no fim.** A wiki envelhece em silêncio e vira mentira. Se em
  algum momento a wiki estiver errada, isso é urgente: consertar página desatualizada tem
  prioridade sobre criar página nova.
- **Segredo em página.** Nunca. Aponte onde a credencial vive, não o valor dela.

---

## 7. Checklist de entrega

Ao terminar, confirme para o usuário, item por item:

- [ ] Hub criado, com `index.md`, `SCHEMA.md`, `CLAUDE.md`, `log.md`, `templates/`, pastas-tema
- [ ] `git init` + primeiro commit no hub
- [ ] `wiki/` criado em cada projeto vivo, com `CLAUDE.md`, `index.md`, `log.md`, `status/`
- [ ] `projetos/mapa-projetos.md` preenchido de verdade (não é esqueleto)
- [ ] Um `status/visao-geral.md` real por projeto vivo
- [ ] Primeira linha de `[ingest]` no `log.md`
- [ ] Rascunho mostrado ao humano com a pergunta: *"o que está errado e o que faltou?"*

E diga a ele a única frase que ele precisa lembrar:
**"No fim de qualquer sessão que decidiu algo, peça: atualize a wiki."**

---

## Notas de origem

Sistema em uso no ecossistema ARCO (hub `arco-wiki` + spokes em cada projeto). Este arquivo é uma
reconstrução do método a partir dos spokes reais — o `SCHEMA.md` original do hub não estava
disponível na máquina onde este arquivo foi gerado, então as regras acima foram derivadas dos
`CLAUDE.md`, `page-template.md` e `log.md` em produção. Se o hub original estiver à mão, ele
manda; use este arquivo como ponto de partida, não como fonte final.
