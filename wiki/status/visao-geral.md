# ABRIU — Visão Geral e Status

> SaaS de rastreamento de orçamentos: "Saiba quando seu cliente abriu o orçamento". Flask no Render, domínio abriu.app.br.

## O que é

SaaS independente da operação ARCO (produto próprio com domínio abriu.app.br). O usuário sobe o
orçamento em PDF, envia um link rastreável ao cliente e recebe notificação no momento da abertura.
Nome antigo do projeto: **OrcEVeja** / "Orce & Veja" (ainda é o nome do produto no Stripe).
Local: `~/projects/orcamentos/` — `app.py` único (~2.300 linhas).

## Como funciona / Como usamos

**Stack:** Flask + gunicorn/gevent no Render (`render.yaml`), PostgreSQL em produção com fallback
SQLite local (`USE_PG` por `DATABASE_URL`), Python 3.11. PWA (manifest + service worker).

**Núcleo:** orçamentos e PDFs com link por token (`/link/<token>`, `/ver/<token>`, `/pdf/<token>`),
tracking de abertura em background (`/track/<token>`), histórico de acessos, renovação de link,
extração de valor do PDF (`/reler_valor`).

**Notificações multicanal:** e-mail, Web Push (VAPID), Telegram (webhook por usuário) e WhatsApp
via Z-API — todos com rota de teste em `/configuracoes`. Dashboard com SSE (tempo real) e pull-to-refresh.

**Monetização (Stripe):** trial de 3 dias no cadastro → paywall (`/paywall`). Planos: **R$19,90/mês**
e **R$180/ano** (produto "Orce & Veja Pro", prices criados sob demanda ou via `STRIPE_PRICE_*`).
Checkout em `/assinar`, webhook em `/webhook/stripe`. Usuários antigos sem `trial_ends_at` têm acesso livre.

**Conta:** login/cadastro + OAuth Gmail. Segurança: CSP, rate limiting com IP real (X-Forwarded-For),
session timeout. Suíte de testes em `tests/` (auth, rotas, webhook Stripe, extração de valor).

## Status

- ✅ Em produção em abriu.app.br (Render, autodeploy).
- ✅ Redesign visual aplicado (tema dark + Geist + coral) — commit ac32ea2.
- ✅ Paywall Stripe completo (trial 3 dias, mensal/anual, webhook testado).
- ⚠️ `DEBUG_REPORT.md` (18/05/2026) aponta pendências de front (css/JS) — conferir o que ainda vale pós-redesign.
- 🔜 Branding: produto no Stripe ainda se chama "Orce & Veja Pro"; alinhar com a marca ABRIU.

## Conexões

- [[projetos/mapa-projetos]] (hub) — posição no ecossistema (produto independente da ARCO)

## Fontes

- `~/projects/orcamentos/app.py`, `render.yaml`, `static/manifest.json`, `DEBUG_REPORT.md`

## Log de atualizações

- 2026-06-11 — criação do spoke a partir de leitura do código (app.py, render.yaml, git log)
