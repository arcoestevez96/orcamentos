import os
import sys
import tempfile
from unittest.mock import MagicMock
import pytest
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash

# ── 1. DB de teste ─────────────────────────────────────────────────────────────
# Deve ser definido ANTES de importar app para que init_db() use o arquivo certo.
_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
os.environ['ORCAMENTOS_DB'] = _db_path
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-pytest')

# ── 2. Mock pdfplumber ─────────────────────────────────────────────────────────
# pdfplumber tem conflito com cryptography neste ambiente; registramos um stub
# em sys.modules para que `import pdfplumber` dentro de extrair_valor_pdf
# retorne nosso mock sem tentar importar a lib real.
_pdfplumber_mock = MagicMock()
sys.modules['pdfplumber'] = _pdfplumber_mock

# ── 3. Import do app ───────────────────────────────────────────────────────────
import app as _app_module

_app_module.init_db()


@pytest.fixture(scope='session')
def app():
    _app_module.app.config.update({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test-secret-key-for-pytest',
        'RATELIMIT_ENABLED': False,       # desativa rate limiting nos testes
    })
    # csrf_token pode ser chamado em templates mesmo com CSRF desativado
    _app_module.app.jinja_env.globals.setdefault('csrf_token', lambda: '')
    yield _app_module.app
    try:
        os.unlink(_db_path)
    except OSError:
        pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def clean_tables():
    # Limpa ANTES do teste para garantir isolamento independente de ordem
    _limpar_tabelas()
    yield
    _limpar_tabelas()


def _limpar_tabelas():
    for table in ('users', 'pdfs', 'pdf_acessos', 'push_subscriptions', 'user_config', 'orcamentos'):
        try:
            _app_module.db_exec(f'DELETE FROM {table}')
        except Exception:
            pass
    # Reset rate limit counters para que testes não interfiram uns nos outros
    try:
        if _app_module.limiter:
            _app_module.limiter._storage.reset()
    except Exception:
        pass


# ── Helpers compartilhados entre testes ───────────────────────────────────────

def make_user(email='usuario@teste.com', senha='senha123', nome='Usuário Teste',
              trial_days=3, status='trial'):
    from zoneinfo import ZoneInfo
    BRASILIA = ZoneInfo('America/Sao_Paulo')
    ph = generate_password_hash(senha)
    trial_end = (datetime.now(BRASILIA) + timedelta(days=trial_days)).strftime('%Y-%m-%d %H:%M:%S')
    _app_module.db_exec(
        'INSERT INTO users(email,password_hash,nome,criado_em,trial_ends_at,subscription_status) '
        'VALUES(?,?,?,?,?,?)',
        (email, ph, nome, _app_module.now_str(), trial_end, status)
    )
    return _app_module.get_user_by_email(email)


def login_client(client, email='usuario@teste.com', senha='senha123'):
    return client.post('/login', data={'email': email, 'senha': senha}, follow_redirects=False)
