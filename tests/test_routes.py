"""Testa rotas públicas, headers de cache e comportamento básico."""


class TestLandingPage:
    def test_retorna_200(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_tem_cache_control_publico(self, client):
        resp = client.get('/')
        cc = resp.headers.get('Cache-Control', '')
        assert 'public' in cc
        assert 'max-age' in cc

    def test_stale_while_revalidate(self, client):
        resp = client.get('/')
        assert 'stale-while-revalidate' in resp.headers.get('Cache-Control', '')

    def test_usuario_logado_redireciona_para_dashboard(self, client):
        from tests.conftest import make_user, login_client
        make_user()
        login_client(client)
        resp = client.get('/', follow_redirects=False)
        assert resp.status_code == 302
        assert '/dashboard' in resp.headers['Location']


class TestHealthEndpoints:
    def test_ping_retorna_200(self, client):
        resp = client.get('/ping')
        assert resp.status_code == 200

    def test_healthz_retorna_200(self, client):
        resp = client.get('/healthz')
        assert resp.status_code == 200


class TestSecurityHeaders:
    def test_x_frame_options_deny(self, client):
        resp = client.get('/')
        assert resp.headers.get('X-Frame-Options') == 'DENY'

    def test_x_content_type_options_nosniff(self, client):
        resp = client.get('/')
        assert resp.headers.get('X-Content-Type-Options') == 'nosniff'

    def test_hsts_presente(self, client):
        resp = client.get('/')
        assert 'max-age' in resp.headers.get('Strict-Transport-Security', '')

    def test_referrer_policy(self, client):
        resp = client.get('/')
        assert resp.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'


class TestStaticAssets:
    def test_css_tem_cache_longo(self, client):
        resp = client.get('/static/css/abriu.css')
        cc = resp.headers.get('Cache-Control', '')
        assert 'max-age=31536000' in cc
        assert 'immutable' in cc

    def test_css_retorna_200(self, client):
        resp = client.get('/static/css/abriu.css')
        assert resp.status_code == 200


class TestTermos:
    def test_retorna_200(self, client):
        resp = client.get('/termos')
        assert resp.status_code == 200

    def test_tem_cache_control(self, client):
        resp = client.get('/termos')
        assert 'public' in resp.headers.get('Cache-Control', '')
