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
