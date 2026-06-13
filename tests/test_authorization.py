"""Testa isolamento entre usuários (IDOR) e exigência de login nas rotas mutadoras.

Cada teste cria dois usuários (dono e intruso) e garante que o intruso não
consegue ler, alterar ou apagar recursos do dono — e que rotas que mutam estado
exigem sessão autenticada.
"""
from unittest.mock import patch
import app as _app_module
from tests.conftest import make_user, login_client

db = _app_module.db_exec


def _criar_pdf(user_id, token='tok-pdf-1'):
    db('INSERT INTO pdfs(token,cliente_nome,titulo,status,criado_em,user_id) '
       'VALUES(?,?,?,?,?,?)',
       (token, 'Cliente A', 'Orçamento A', 'enviado', _app_module.now_str(), user_id))
    return db('SELECT id FROM pdfs WHERE token=?', (token,), fetch='one')['id']


def _criar_orcamento(user_id, token='tok-orc-1'):
    db('INSERT INTO orcamentos(token,cliente_nome,titulo,itens,status,criado_em,user_id) '
       'VALUES(?,?,?,?,?,?,?)',
       (token, 'Cliente A', 'Orçamento A', '[]', 'rascunho', _app_module.now_str(), user_id))
    return db('SELECT id FROM orcamentos WHERE token=?', (token,), fetch='one')['id']


class TestIDORPdf:
    def test_intruso_nao_deleta_pdf_alheio(self, client):
        dono = make_user(email='dono@teste.com')
        make_user(email='intruso@teste.com')
        pdf_id = _criar_pdf(dono['id'])
        login_client(client, 'intruso@teste.com')

        resp = client.post(f'/deletar_pdf_ajax/{pdf_id}')

        assert resp.status_code == 404
        assert db('SELECT id FROM pdfs WHERE id=?', (pdf_id,), fetch='one') is not None

    def test_dono_deleta_proprio_pdf(self, client):
        dono = make_user(email='dono@teste.com')
        pdf_id = _criar_pdf(dono['id'])
        login_client(client, 'dono@teste.com')

        resp = client.post(f'/deletar_pdf_ajax/{pdf_id}')

        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True
        assert db('SELECT id FROM pdfs WHERE id=?', (pdf_id,), fetch='one') is None

    def test_intruso_nao_le_valor_de_pdf_alheio(self, client):
        dono = make_user(email='dono@teste.com')
        make_user(email='intruso@teste.com')
        pdf_id = _criar_pdf(dono['id'])
        login_client(client, 'intruso@teste.com')

        resp = client.post(f'/reler_valor/{pdf_id}')

        assert resp.status_code == 404
        assert resp.get_json()['ok'] is False


class TestIDOROrcamento:
    def test_intruso_nao_edita_orcamento_alheio(self, client):
        dono = make_user(email='dono@teste.com')
        make_user(email='intruso@teste.com')
        orc_id = _criar_orcamento(dono['id'])
        login_client(client, 'intruso@teste.com')

        client.post(f'/editar/{orc_id}', json={
            'cliente_nome': 'HACKED', 'titulo': 'HACKED', 'itens': []})

        row = db('SELECT cliente_nome, titulo FROM orcamentos WHERE id=?', (orc_id,), fetch='one')
        assert row['cliente_nome'] == 'Cliente A'
        assert row['titulo'] == 'Orçamento A'

    def test_intruso_nao_deleta_orcamento_alheio(self, client):
        dono = make_user(email='dono@teste.com')
        make_user(email='intruso@teste.com')
        orc_id = _criar_orcamento(dono['id'])
        login_client(client, 'intruso@teste.com')

        client.post(f'/deletar/{orc_id}')

        assert db('SELECT id FROM orcamentos WHERE id=?', (orc_id,), fetch='one') is not None

    def test_intruso_nao_abre_editor_de_orcamento_alheio(self, client):
        dono = make_user(email='dono@teste.com')
        make_user(email='intruso@teste.com')
        orc_id = _criar_orcamento(dono['id'])
        login_client(client, 'intruso@teste.com')

        resp = client.get(f'/editar/{orc_id}', follow_redirects=False)

        assert resp.status_code == 302
        assert '/dashboard' in resp.headers['Location']


class TestLoginObrigatorio:
    def test_criar_exige_login(self, client):
        resp = client.post('/criar', json={'cliente_nome': 'x', 'titulo': 'y', 'itens': []},
                           follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']

    def test_deletar_orcamento_exige_login(self, client):
        resp = client.post('/deletar/1', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']

    def test_atualizar_status_exige_login(self, client):
        resp = client.post('/atualizar_status/1', json={'status': 'fechou'},
                           follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']

    def test_testar_whatsapp_exige_login(self, client):
        resp = client.post('/testar_whatsapp', json={'numero': '1', 'apikey': '1'},
                           follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def _chat_id_salvo(uid):
    r = db('SELECT valor FROM user_config WHERE user_id=? AND chave=?',
           (uid, 'telegram_chat_id'), fetch='one')
    return r['valor'] if r else None


class TestTelegramWebhookPorUsuario:
    """O webhook por usuário não pode aceitar chat_id sem o secret_token correto."""
    def test_rejeita_sem_secret_token(self, client):
        dono = make_user(email='tg@teste.com')
        _app_module.save_user_config(dono['id'], {'telegram_webhook_secret': 's3cr3t', 'telegram_token': 'bot'})
        r = client.post(f"/telegram/webhook/{dono['id']}", json={'message': {'chat': {'id': 999}}})
        assert r.status_code == 403
        assert _chat_id_salvo(dono['id']) is None

    def test_aceita_com_secret_correto(self, client):
        dono = make_user(email='tg2@teste.com')
        _app_module.save_user_config(dono['id'], {'telegram_webhook_secret': 's3cr3t', 'telegram_token': 'bot'})
        with patch('app.requests.post'):  # não chama o Telegram de verdade
            r = client.post(f"/telegram/webhook/{dono['id']}",
                            json={'message': {'chat': {'id': 999}}},
                            headers={'X-Telegram-Bot-Api-Secret-Token': 's3cr3t'})
        assert r.status_code == 200
        assert _chat_id_salvo(dono['id']) == '999'
