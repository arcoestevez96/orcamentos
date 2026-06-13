"""Testa rotas de autenticação: login, cadastro e logout."""
from unittest.mock import patch

from tests.conftest import make_user, login_client


class TestLoginPage:
    def test_get_retorna_200(self, client):
        make_user()
        resp = client.get('/login')
        assert resp.status_code == 200

    def test_sem_usuarios_redireciona_para_cadastro(self, client):
        resp = client.get('/login', follow_redirects=False)
        assert resp.status_code == 302
        assert '/cadastro' in resp.headers['Location']

    def test_senha_errada_exibe_erro(self, client):
        make_user()
        resp = client.post('/login', data={'email': 'usuario@teste.com', 'senha': 'errada'},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert 'incorretos' in resp.data.decode('utf-8')

    def test_email_inexistente_exibe_erro(self, client):
        make_user()
        resp = client.post('/login', data={'email': 'nao@existe.com', 'senha': 'senha123'},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert 'incorretos' in resp.data.decode('utf-8')

    def test_credenciais_corretas_redireciona_para_dashboard(self, client):
        make_user()
        resp = login_client(client)
        assert resp.status_code == 302
        assert '/dashboard' in resp.headers['Location']

    def test_sessao_e_definida_apos_login(self, client, app):
        make_user()
        with client:
            login_client(client)
            with client.session_transaction() as sess:
                assert sess.get('user_email') == 'usuario@teste.com'


class TestCadastro:
    def test_get_retorna_200(self, client):
        resp = client.get('/cadastro')
        assert resp.status_code == 200

    def test_cadastro_valido_cria_usuario(self, client):
        with patch('app.notificar_admin_novo_usuario'):
            resp = client.post('/cadastro', data={
                'nome': 'Novo Usuário',
                'email': 'novo@teste.com',
                'senha': 'senha123',
                'confirmar': 'senha123',
            }, follow_redirects=False)
        assert resp.status_code == 302
        import app as _app
        u = _app.get_user_by_email('novo@teste.com')
        assert u is not None
        assert u['nome'] == 'Novo Usuário'

    def test_cadastro_valido_define_trial(self, client):
        with patch('app.notificar_admin_novo_usuario'):
            client.post('/cadastro', data={
                'nome': 'Novo Usuário',
                'email': 'trial@teste.com',
                'senha': 'senha123',
                'confirmar': 'senha123',
            })
        import app as _app
        u = _app.get_user_by_email('trial@teste.com')
        assert u['subscription_status'] == 'trial'
        assert u['trial_ends_at'] is not None

    def test_senhas_diferentes_exibe_erro(self, client):
        resp = client.post('/cadastro', data={
            'nome': 'Teste',
            'email': 'teste@teste.com',
            'senha': 'senha123',
            'confirmar': 'outra123',
        }, follow_redirects=True)
        assert 'coincidem' in resp.data.decode('utf-8')

    def test_senha_curta_exibe_erro(self, client):
        resp = client.post('/cadastro', data={
            'nome': 'Teste',
            'email': 'teste@teste.com',
            'senha': '123',
            'confirmar': '123',
        }, follow_redirects=True)
        assert '8 caracteres' in resp.data.decode('utf-8')

    def test_email_duplicado_exibe_erro(self, client):
        make_user()
        resp = client.post('/cadastro', data={
            'nome': 'Cópia',
            'email': 'usuario@teste.com',
            'senha': 'senha123',
            'confirmar': 'senha123',
        }, follow_redirects=True)
        assert 'já está cadastrado' in resp.data.decode('utf-8')

    def test_email_e_senha_obrigatorios(self, client):
        resp = client.post('/cadastro', data={
            'nome': 'Teste',
            'email': '',
            'senha': '',
            'confirmar': '',
        }, follow_redirects=True)
        assert 'email e senha' in resp.data.decode('utf-8')

    def test_usuario_logado_redireciona_para_dashboard(self, client):
        make_user()
        login_client(client)
        resp = client.get('/cadastro', follow_redirects=False)
        assert resp.status_code == 302
        assert '/dashboard' in resp.headers['Location']


class TestLogout:
    def test_logout_limpa_sessao(self, client, app):
        make_user()
        login_client(client)
        with client:
            client.get('/logout')
            with client.session_transaction() as sess:
                assert 'user_email' not in sess

    def test_logout_redireciona_para_login(self, client):
        make_user()
        login_client(client)
        resp = client.get('/logout', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


class TestLoginRequired:
    def test_dashboard_sem_login_redireciona(self, client):
        resp = client.get('/dashboard', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']

    def test_configuracoes_sem_login_redireciona(self, client):
        resp = client.get('/configuracoes', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']

    def test_trial_expirado_redireciona_para_paywall(self, client):
        make_user(trial_days=-1)
        login_client(client)
        resp = client.get('/dashboard', follow_redirects=False)
        assert resp.status_code == 302
        assert '/paywall' in resp.headers['Location']
