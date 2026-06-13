# log.md — Diário do ABRIU (orcamentos)

> Append-only. `AAAA-MM-DD [tipo]`. Tipos: ingest | query | lint | decisao

---

## 2026-06-11 [tipo: ingest]

Fonte: leitura do código (`app.py`, `render.yaml`, `git log`, `DEBUG_REPORT.md`).
Página criada: [[status/visao-geral]]. Spoke criado — projeto antes estava fora da wiki
("fora do spoke" no mapa do hub). Confirmado: Stripe/paywall existe (trial 3 dias, R$19,90/mês, R$180/ano).

## 2026-06-12 [tipo: decisao]

Auditoria de segurança + correção de brechas IDOR (multi-tenant). Corrigido em `app.py`:
- `/deletar_pdf_ajax/<id>` e `/reler_valor/<id>`: faltava filtro `user_id` → qualquer usuário
  logado apagava/lia PDF alheio. Agora filtram por dono e retornam 404 se não for dele.
- Builder de orçamentos (`/criar`, `/editar`, `/deletar`, `/marcar_enviado`, `/atualizar_status`,
  `/link`): estavam SEM `@login_required` e a tabela `orcamentos` não tinha `user_id`.
  Adicionada coluna `user_id` (migração PG + SQLite) + `@login_required` + filtro por dono.
- Gmail OAuth migrado de `config` global para `user_config` (era single-tenant: 1 conta Gmail
  para toda a plataforma). Rotas `/auth/gmail/callback`, `/auth/gmail/disconnect`, `/gmail/status`,
  `/testar_email` agora por usuário.
- `@login_required` adicionado aos testes de canal (`/testar_whatsapp|zapi|telegram|email`) —
  evitavam relay anônimo.
Testes: novo `tests/test_authorization.py` (10 testes IDOR/login, todos verdes). Suíte: 84 passam,
2 falham só por `hashlib.scrypt` ausente no Python do host (CommandLineTools) — passam em prod (3.11).
`tests/conftest.py`: `make_user` usa pbkdf2 para não depender do scrypt do host.

## 2026-06-13 [tipo: decisao]

Performance + notificações + deploy (commits 05a3a4e, d6fb993, 4de949d, no ar em abriu.app.br):
- **Perf (maior ganho):** webfont de ícones Tabler (247KB CSS + 820KB woff2 via CDN, em TODA
  página, p/ usar só 35 ícones) substituído por `static/css/icons.css` — os 35 SVGs via
  `mask-image`, auto-hospedado, gzip + cache 1 ano. ~1MB → 52KB/página. TTFB ~0,3s.
- **Web Push:** card "Ativar notificações" no dashboard + `ativarPush()` que PEDE permissão
  (antes o código só rodava se já concedida → nunca ativava). Canal de fricção zero.
- **Deploy:** Render autodeploy estava OFF — deploys precisam ser disparados manualmente no
  painel (Manual Deploy → Deploy latest commit) OU via Deploy Hook (não temos a URL salva).
- **Cold start (causa nº2 da lentidão):** plano **free do Render hiberna após ~15min** (banner
  confirma "delay 50s+"). Resolvido com **keep-warm via cron-job.org** batendo em
  `https://orcamentos-bqf1.onrender.com/ping` a cada 10min (200 OK). Servidor fica sempre quente.
  Upgrade Starter US$7/mo elimina spin-down de vez (futuro, quando faturar).

## 2026-06-13 [tipo: decisao]

Auditoria profunda + Fase 1 (blindar p/ vender) + Fase 2 (mais valor). Commits 73fd8f8, 42ab5e5.
Auditoria confirmou: zero SQL injection, zero RCE, headers fortes. Achados novos corrigidos:
- **Stripe webhook** agora EXIGE `STRIPE_WEBHOOK_SECRET` (sem ele → 503). Fechava bypass de
  assinatura grátis. **AÇÃO PENDENTE:** setar `STRIPE_WEBHOOK_SECRET` no Render antes do deploy.
- **Telegram** webhook por usuário `/telegram/webhook/<uid>` + secret_token; corrige bug LIMIT 1
  (chat_id ia pro vendedor errado com 2+ usuários). Rota legada mantida como fallback.
- Falhas silenciosas de notificação agora logam; OAuth Gmail com `state` anti-CSRF; senha mín 8;
  marca unificada ABRIU (admin/e-mails/Stripe).
- **Fase 2 — aceite:** rota pública `/aceitar/<token>` (pdfs+orcamentos), `aceite.html`, grava
  `decisao`/`decisao_em`, idempotente, notifica o dono. Link de aceite no e-mail e WhatsApp.
- **Fase 2 — lead scoring:** selo Quente(≥3 aberturas)/Morno(1-2)/Frio(0) + Aceito/Recusou no
  dashboard. Testes: 90 verdes (2 falham só por scrypt ausente no host).
Pendente p/ fechar venda: (a) STRIPE_WEBHOOK_SECRET no Render, (b) deploy manual (autodeploy off),
(c) teste de notificação ponta-a-ponta com um canal real.
