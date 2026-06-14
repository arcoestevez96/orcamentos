# gevent monkey-patch deve ser a primeira coisa — antes de qualquer import
try:
    from gevent import monkey as _gm; _gm.patch_all()
except ImportError:
    pass

import logging, os, json, uuid, requests, smtplib, threading, re, io, base64, queue, secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
log = logging.getLogger('abriu')

BRASILIA = ZoneInfo('America/Sao_Paulo')
from flask import Flask, render_template, request, redirect, jsonify, send_file, Response, session, url_for, flash
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# ── SECRET_KEY ────────────────────────────────────────────────────────────────
_sk = os.environ.get('SECRET_KEY')
if not _sk:
    _sk = secrets.token_hex(32)
    log.critical('SECRET_KEY nao configurada — usando chave temporaria. '
                 'Sessoes serao perdidas a cada restart. '
                 'Configure SECRET_KEY em Render > Settings > Environment Variables.')
app.secret_key = _sk

# ── Cookies de sessão seguros ─────────────────────────────────────────────────
app.config['SESSION_COOKIE_SECURE']    = True
app.config['SESSION_COOKIE_HTTPONLY']  = True
app.config['SESSION_COOKIE_SAMESITE']  = 'Lax'
app.config['SESSION_COOKIE_NAME']      = '__Host-session'
app.config['PERMANENT_SESSION_LIFETIME'] = 1800  # 30 min inativo → logout automático

# ── Gzip ──────────────────────────────────────────────────────────────────────
try:
    from flask_compress import Compress
    app.config['COMPRESS_MIMETYPES'] = [
        'text/html','text/css','application/javascript',
        'application/json','text/plain'
    ]
    app.config['COMPRESS_LEVEL'] = 6
    app.config['COMPRESS_MIN_SIZE'] = 500
    Compress(app)
except ImportError:
    pass

# ── CSRF ──────────────────────────────────────────────────────────────────────
try:
    from flask_wtf.csrf import CSRFProtect
    csrf = CSRFProtect(app)
    app.config['WTF_CSRF_TIME_LIMIT'] = 3600
except ImportError:
    csrf = None
    log.warning('flask-wtf não instalado — proteção CSRF desativada')

# ── Rate Limiting ─────────────────────────────────────────────────────────────
def _real_ip():
    """Retorna o IP real do cliente respeitando X-Forwarded-For do Render/proxy."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'

try:
    from flask_limiter import Limiter
    limiter = Limiter(app=app, key_func=_real_ip,
                      default_limits=[], storage_uri='memory://')
except ImportError:
    limiter = None
    log.warning('flask-limiter não instalado — rate limiting desativado')

DATABASE_URL = os.environ.get('DATABASE_URL')
USE_PG = bool(DATABASE_URL)

def csrf_exempt(f):
    """Marca uma rota como isenta de CSRF (ex: webhooks externos)."""
    return csrf.exempt(f) if csrf else f

# ── Redireciona www → apex ────────────────────────────────────────────────────
@app.before_request
def redirect_www():
    host = request.host.split(':')[0]
    if host.startswith('www.'):
        apex = host[4:]
        url = request.url.replace(f'https://{host}', f'https://{apex}', 1) \
                         .replace(f'http://{host}',  f'https://{apex}', 1)
        return redirect(url, code=301)

@app.before_request
def renovar_sessao():
    """Renova o timer de 30min a cada request autenticado."""
    if session.get('user_email'):
        session.permanent = True
        session.modified   = True

# ── Headers de cache + segurança ──────────────────────────────────────────────
@app.after_request
def set_response_headers(response):
    path = request.path
    if path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif path == '/sw.js':
        response.headers['Cache-Control'] = 'no-cache'
    elif path in ('/', '/termos', '/robots.txt', '/sitemap.xml'):
        response.headers['Cache-Control'] = 'public, max-age=300, stale-while-revalidate=3600'
    response.headers['X-Frame-Options']        = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains; preload'
    response.headers['X-Permitted-Cross-Domain-Policies'] = 'none'
    response.headers['Cross-Origin-Opener-Policy']  = 'same-origin'
    response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    response.headers['Permissions-Policy'] = (
        'camera=(), microphone=(), geolocation=(), '
        'payment=(), usb=(), interest-cohort=()'
    )
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://js.stripe.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://api.stripe.com; "
        "frame-src https://js.stripe.com https://hooks.stripe.com; "
        "frame-ancestors 'none';"
    )
    # Páginas autenticadas não devem ser cacheadas por proxies
    if path.startswith(('/dashboard', '/configuracoes', '/upload', '/renovar', '/acessos')):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
    return response


# ── SSE — filas por usuário ───────────────────────────────────────────────────
_sse_lock   = threading.Lock()
_sse_queues = {}   # user_id (int) -> list[queue.Queue]

def sse_push(user_id, event, data):
    """Envia evento SSE apenas para as conexões do usuário dono do PDF."""
    with _sse_lock:
        filas = list(_sse_queues.get(int(user_id), []))
    for q in filas:
        try:
            q.put_nowait({'event': event, 'data': data})
        except queue.Full:
            pass

# ── Web Push (VAPID) ──────────────────────────────────────────────────────────

def get_vapid_keys():
    """Retorna (private_key_pem, public_key_b64url). Gera na primeira chamada."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        # Verifica env
        priv = os.environ.get('VAPID_PRIVATE_KEY', '')
        pub  = os.environ.get('VAPID_PUBLIC_KEY', '')
        if priv and pub:
            return priv, pub
        # Verifica config
        cfg  = get_config()
        priv = cfg.get('vapid_private_key', '')
        pub  = cfg.get('vapid_public_key', '')
        if priv and pub:
            return priv, pub
        # Gera par de chaves ECDH P-256
        private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')
        pub_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint
        )
        pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode('ascii')
        save_config({'vapid_private_key': pem, 'vapid_public_key': pub_b64})
        return pem, pub_b64
    except Exception as e:
        log.error('VAPID key error: %s', e)
        return '', ''

def send_web_push(user_id, title, body, url='/dashboard'):
    """Envia push notification para as subscriptions do usuário (background thread)."""
    def _worker():
        try:
            from pywebpush import webpush, WebPushException
            priv_key, _ = get_vapid_keys()
            if not priv_key:
                return
            sql = 'SELECT id, subscription_json FROM push_subscriptions WHERE user_id=%s' if USE_PG else \
                  'SELECT id, subscription_json FROM push_subscriptions WHERE user_id=?'
            rows = db_exec(sql, (user_id,), fetch='all') or []
            payload = json.dumps({'title': title, 'body': body, 'url': url}, ensure_ascii=False)
            stale = []
            for row in rows:
                try:
                    sub = json.loads(row['subscription_json'])
                    webpush(
                        subscription_info=sub,
                        data=payload,
                        vapid_private_key=priv_key,
                        vapid_claims={'sub': 'mailto:noreply@abriu.app.br'}
                    )
                except Exception as e:
                    resp = getattr(e, 'response', None)
                    if resp and resp.status_code in (404, 410):
                        stale.append(row['id'])
            for sid in stale:
                db_exec('DELETE FROM push_subscriptions WHERE id=%s' if USE_PG else
                        'DELETE FROM push_subscriptions WHERE id=?', (sid,))
        except Exception as ex:
            log.error('Web push error: %s', ex)
    threading.Thread(target=_worker, daemon=True).start()

# ── banco de dados ──────────────────────────────────────────────────────────

_pg_pool = None

def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        import psycopg2.pool
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(2, 10, url)
    return _pg_pool

def _reset_pg_pool():
    """Fecha todas as conexões do pool e força recriação na próxima chamada."""
    global _pg_pool
    old, _pg_pool = _pg_pool, None
    if old:
        try: old.closeall()
        except Exception: pass

def get_db():
    if USE_PG:
        return _get_pg_pool().getconn()
    else:
        import sqlite3
        db_path = os.environ.get('ORCAMENTOS_DB',
                                  os.path.join(os.path.dirname(__file__), 'orcamentos.db'))
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return con

def db_exec(sql, params=(), fetch=None):
    for attempt in range(2):
        con = None
        pool = None
        ok = False
        try:
            if USE_PG:
                pool = _get_pg_pool()
                con = pool.getconn()
                import psycopg2.extras
                cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                sql_pg = sql.replace('?', '%s').replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
                cur.execute(sql_pg, params)
                con.commit()
                ok = True
                if fetch == 'all': return [dict(r) for r in cur.fetchall()]
                if fetch == 'one': r = cur.fetchone(); return dict(r) if r else None
                return None
            else:
                con = get_db()
                cur = con.execute(sql, params)
                con.commit()
                if fetch == 'all': return [dict(r) for r in cur.fetchall()]
                if fetch == 'one': r = cur.fetchone(); return dict(r) if r else None
                return None
        except Exception as e:
            if con and USE_PG:
                try: con.rollback()
                except Exception: pass
            # Conexões SSL fechadas pelo servidor são recuperáveis — descarta o pool e tenta uma vez
            if attempt == 0 and USE_PG:
                import psycopg2
                if isinstance(e, psycopg2.OperationalError):
                    if con and pool:
                        try: pool.putconn(con, close=True)
                        except Exception: pass
                    con = None  # impede o finally de devolver conexão ruim ao pool
                    _reset_pg_pool()
                    log.warning('db_exec: conexão SSL morta detectada, reconectando...')
                    continue
            raise
        finally:
            if con:
                if USE_PG and pool:
                    pool.putconn(con, close=not ok)
                else:
                    con.close()

def init_db():
    if USE_PG:
        import psycopg2
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        con = psycopg2.connect(url)
        cur = con.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS orcamentos (
                id SERIAL PRIMARY KEY, token TEXT UNIQUE NOT NULL,
                cliente_nome TEXT NOT NULL, cliente_telefone TEXT, titulo TEXT NOT NULL,
                itens TEXT NOT NULL, observacoes TEXT, prazo TEXT,
                forma_pagamento TEXT, validade TEXT, status TEXT DEFAULT 'rascunho',
                criado_em TEXT NOT NULL, aberto_em TEXT
            )''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT)''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS pdfs (
                id SERIAL PRIMARY KEY, token TEXT UNIQUE NOT NULL,
                cliente_nome TEXT NOT NULL, cliente_telefone TEXT, titulo TEXT NOT NULL,
                arquivo BYTEA, filename TEXT, status TEXT DEFAULT 'enviado',
                criado_em TEXT NOT NULL, aberto_em TEXT, aberturas INTEGER DEFAULT 0,
                valor NUMERIC DEFAULT 0
            )''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                nome TEXT,
                criado_em TEXT NOT NULL,
                oauth_provider TEXT,
                oauth_id TEXT,
                foto TEXT
            )''')
        cur.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS oauth_provider TEXT')
        cur.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS oauth_id TEXT')
        cur.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS foto TEXT')
        cur.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_ends_at TEXT')
        cur.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT \'trial\'')
        cur.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_ends_at TEXT')
        cur.execute('ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS valor NUMERIC DEFAULT 0')
        cur.execute('ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS user_id INTEGER')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS pdf_acessos (
                id SERIAL PRIMARY KEY,
                pdf_id INTEGER,
                token TEXT,
                ip TEXT,
                user_agent TEXT,
                dispositivo TEXT,
                sistema TEXT,
                cidade TEXT,
                regiao TEXT,
                pais TEXT,
                operadora TEXT,
                fonte TEXT,
                criado_em TEXT
            )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            subscription_json TEXT NOT NULL,
            endpoint TEXT,
            criado_em TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS user_config (
            user_id INTEGER NOT NULL,
            chave   TEXT NOT NULL,
            valor   TEXT,
            PRIMARY KEY (user_id, chave)
        )''')
        # Índices para consultas críticas (token lookup e filtro por usuário)
        cur.execute('CREATE INDEX IF NOT EXISTS idx_pdfs_token   ON pdfs(token)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_pdfs_user    ON pdfs(user_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_acessos_pdf  ON pdf_acessos(pdf_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_push_user    ON push_subscriptions(user_id)')
        for k, v in [('whatsapp_numero',''),('whatsapp_apikey',''),
                     ('empresa_nome',''),('base_url',''),
                     ('email_remetente',''),('email_senha_app',''),
                     ('telegram_token',''),('telegram_chat_id',''),
                     ('zapi_instance',''),('zapi_token',''),('zapi_client_token',''),('zapi_phone',''),
                     ('gmail_refresh_token',''),('gmail_email','')]:
            cur.execute('INSERT INTO config(chave,valor) VALUES(%s,%s) ON CONFLICT DO NOTHING', (k, v))
        cur.execute('ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS arquivo_key TEXT')
        cur.execute('ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS cliente_email TEXT')
        cur.execute('ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS decisao TEXT')
        cur.execute('ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS decisao_em TEXT')
        cur.execute('ALTER TABLE orcamentos ADD COLUMN IF NOT EXISTS user_id INTEGER')
        cur.execute('ALTER TABLE orcamentos ADD COLUMN IF NOT EXISTS decisao TEXT')
        cur.execute('ALTER TABLE orcamentos ADD COLUMN IF NOT EXISTS decisao_em TEXT')
        cur.execute('ALTER TABLE pdf_acessos ADD COLUMN IF NOT EXISTS fonte TEXT')
        cur.execute('ALTER TABLE pdf_acessos ADD COLUMN IF NOT EXISTS operadora TEXT')
        cur.execute('ALTER TABLE pdf_acessos ADD COLUMN IF NOT EXISTS sistema TEXT')
        cur.execute('ALTER TABLE pdf_acessos ADD COLUMN IF NOT EXISTS dispositivo TEXT')
        for idx_sql in [
            'CREATE INDEX IF NOT EXISTS idx_pdfs_user_id ON pdfs(user_id)',
            'CREATE INDEX IF NOT EXISTS idx_pdfs_token ON pdfs(token)',
            'CREATE INDEX IF NOT EXISTS idx_pdf_acessos_pdf_id ON pdf_acessos(pdf_id)',
            'CREATE INDEX IF NOT EXISTS idx_user_config_user_id ON user_config(user_id)',
            'CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)',
            'CREATE INDEX IF NOT EXISTS idx_orcamentos_token ON orcamentos(token)',
        ]:
            try: cur.execute(idx_sql)
            except Exception: pass
        con.commit()
        con.close()
    else:
        import sqlite3
        db_path = os.environ.get('ORCAMENTOS_DB',
                                  os.path.join(os.path.dirname(__file__), 'orcamentos.db'))
        con = sqlite3.connect(db_path)
        con.executescript('''
            CREATE TABLE IF NOT EXISTS orcamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
                cliente_nome TEXT NOT NULL, cliente_telefone TEXT, titulo TEXT NOT NULL,
                itens TEXT NOT NULL, observacoes TEXT, prazo TEXT,
                forma_pagamento TEXT, validade TEXT, status TEXT DEFAULT 'rascunho',
                criado_em TEXT NOT NULL, aberto_em TEXT
            );
            CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                nome TEXT,
                criado_em TEXT NOT NULL,
                oauth_provider TEXT,
                oauth_id TEXT,
                foto TEXT,
                trial_ends_at TEXT,
                subscription_status TEXT DEFAULT 'trial',
                subscription_ends_at TEXT
            );
            CREATE TABLE IF NOT EXISTS pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
                cliente_nome TEXT NOT NULL, cliente_telefone TEXT, titulo TEXT NOT NULL,
                arquivo BLOB, filename TEXT, status TEXT DEFAULT 'enviado',
                criado_em TEXT NOT NULL, aberto_em TEXT, aberturas INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pdf_acessos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pdf_id INTEGER,
                token TEXT,
                ip TEXT,
                user_agent TEXT,
                dispositivo TEXT,
                sistema TEXT,
                cidade TEXT,
                regiao TEXT,
                pais TEXT,
                operadora TEXT,
                fonte TEXT,
                criado_em TEXT
            );
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subscription_json TEXT NOT NULL,
                endpoint TEXT,
                criado_em TEXT
            );
            CREATE TABLE IF NOT EXISTS user_config (
                user_id INTEGER NOT NULL,
                chave   TEXT NOT NULL,
                valor   TEXT,
                PRIMARY KEY (user_id, chave)
            );
            INSERT OR IGNORE INTO config VALUES ("whatsapp_numero","");
            INSERT OR IGNORE INTO config VALUES ("whatsapp_apikey","");
            INSERT OR IGNORE INTO config VALUES ("empresa_nome","");
            INSERT OR IGNORE INTO config VALUES ("base_url","");
            INSERT OR IGNORE INTO config VALUES ("email_remetente","");
            INSERT OR IGNORE INTO config VALUES ("email_senha_app","");
            INSERT OR IGNORE INTO config VALUES ("telegram_token","");
            INSERT OR IGNORE INTO config VALUES ("telegram_chat_id","");
            INSERT OR IGNORE INTO config VALUES ("zapi_instance","");
            INSERT OR IGNORE INTO config VALUES ("zapi_token","");
            INSERT OR IGNORE INTO config VALUES ("zapi_client_token","");
            INSERT OR IGNORE INTO config VALUES ("zapi_phone","");
            INSERT OR IGNORE INTO config VALUES ("gmail_refresh_token","");
            INSERT OR IGNORE INTO config VALUES ("gmail_email","");
            CREATE INDEX IF NOT EXISTS idx_pdfs_token  ON pdfs(token);
            CREATE INDEX IF NOT EXISTS idx_acessos_pdf ON pdf_acessos(pdf_id);
            CREATE INDEX IF NOT EXISTS idx_push_user   ON push_subscriptions(user_id);
        ''')
        # Migração: adicionar user_id à tabela pdfs (SQLite não suporta IF NOT EXISTS)
        try:
            con.execute('ALTER TABLE pdfs ADD COLUMN user_id INTEGER')
            con.commit()
        except Exception:
            pass
        for col_sql in [
            'ALTER TABLE pdfs ADD COLUMN arquivo_key TEXT',
            'ALTER TABLE pdfs ADD COLUMN valor REAL DEFAULT 0',
            'ALTER TABLE pdfs ADD COLUMN cliente_email TEXT',
            'ALTER TABLE pdfs ADD COLUMN decisao TEXT',
            'ALTER TABLE pdfs ADD COLUMN decisao_em TEXT',
            'ALTER TABLE orcamentos ADD COLUMN user_id INTEGER',
            'ALTER TABLE orcamentos ADD COLUMN decisao TEXT',
            'ALTER TABLE orcamentos ADD COLUMN decisao_em TEXT',
        ]:
            try: con.execute(col_sql)
            except Exception: pass
        for idx_sql in [
            'CREATE INDEX IF NOT EXISTS idx_pdfs_user_id ON pdfs(user_id)',
            'CREATE INDEX IF NOT EXISTS idx_pdfs_token ON pdfs(token)',
            'CREATE INDEX IF NOT EXISTS idx_pdf_acessos_pdf_id ON pdf_acessos(pdf_id)',
            'CREATE INDEX IF NOT EXISTS idx_user_config_user_id ON user_config(user_id)',
            'CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)',
            'CREATE INDEX IF NOT EXISTS idx_orcamentos_token ON orcamentos(token)',
        ]:
            try: con.execute(idx_sql)
            except Exception: pass
        con.commit()
        con.close()

def get_config():
    rows = db_exec('SELECT chave, valor FROM config', fetch='all')
    return {r['chave']: r['valor'] for r in rows} if rows else {}

# ── Cloudflare R2 (armazenamento de PDFs) ────────────────────────────────────

def r2_configured():
    return bool(os.environ.get('R2_ACCOUNT_ID') and
                os.environ.get('R2_ACCESS_KEY_ID') and
                os.environ.get('R2_BUCKET_NAME'))

def _r2_client():
    import boto3
    account_id = os.environ.get('R2_ACCOUNT_ID', '')
    return boto3.client(
        's3',
        endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID', ''),
        aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY', ''),
        region_name='auto',
    )

def r2_upload(key: str, data: bytes):
    _r2_client().put_object(
        Bucket=os.environ.get('R2_BUCKET_NAME', ''),
        Key=key, Body=data, ContentType='application/pdf')

def r2_download(key: str) -> bytes:
    resp = _r2_client().get_object(
        Bucket=os.environ.get('R2_BUCKET_NAME', ''), Key=key)
    return resp['Body'].read()

def r2_delete(key: str):
    try:
        _r2_client().delete_object(
            Bucket=os.environ.get('R2_BUCKET_NAME', ''), Key=key)
    except Exception:
        pass

def r2_url(key: str, expires: int = 7200) -> str:
    """Retorna URL pública (R2_PUBLIC_URL) ou presigned URL com TTL em segundos."""
    pub = os.environ.get('R2_PUBLIC_URL', '').rstrip('/')
    if pub:
        return f'{pub}/{key}'
    return _r2_client().generate_presigned_url(
        'get_object',
        Params={'Bucket': os.environ.get('R2_BUCKET_NAME', ''), 'Key': key},
        ExpiresIn=expires,
    )

# ── autenticação ─────────────────────────────────────────────────────────────

def usuario_existe():
    r = db_exec('SELECT id FROM users LIMIT 1', fetch='one')
    return r is not None

def get_user_by_email(email):
    sql = 'SELECT * FROM users WHERE email=%s' if USE_PG else 'SELECT * FROM users WHERE email=?'
    return db_exec(sql, (email,), fetch='one')

def has_access(user):
    """True se usuário tem trial ativo ou assinatura ativa."""
    if not user:
        return False
    status = user.get('subscription_status') or 'trial'
    if status == 'ativo':
        ends = user.get('subscription_ends_at')
        if ends:
            try:
                end = datetime.strptime(ends[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BRASILIA)
                return datetime.now(BRASILIA) <= end
            except Exception:
                pass
        return True
    # Verifica período de trial
    trial_ends = user.get('trial_ends_at')
    if not trial_ends:
        return True  # usuários antigos sem trial_ends_at têm acesso livre
    try:
        end = datetime.strptime(trial_ends[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BRASILIA)
        return datetime.now(BRASILIA) <= end
    except Exception:
        return True

def dias_trial_restantes(user):
    if not user or user.get('subscription_status') == 'ativo':
        return None
    trial_ends = user.get('trial_ends_at')
    if not trial_ends:
        return None
    try:
        end = datetime.strptime(trial_ends[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BRASILIA)
        delta = end - datetime.now(BRASILIA)
        return max(0, delta.days)
    except Exception:
        return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import g
        if not session.get('user_email'):
            return redirect(url_for('login'))
        u = get_user_by_email(session['user_email'])
        if not has_access(u):
            return redirect(url_for('paywall'))
        g.current_user = u
        return f(*args, **kwargs)
    return decorated

def _rate_limit(limit_str):
    """Aplica rate limiting se flask-limiter estiver disponível, senão no-op."""
    def decorator(f):
        if limiter:
            return limiter.limit(limit_str)(f)
        return f
    return decorator

@app.route('/login', methods=['GET', 'POST'])
@_rate_limit('10 per minute')
def login():
    # Se ainda não tem usuário cadastrado, redireciona para cadastro
    if not usuario_existe():
        return redirect(url_for('cadastro'))
    erro = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '').strip()
        sql = 'SELECT * FROM users WHERE email=%s' if USE_PG else 'SELECT * FROM users WHERE email=?'
        u = db_exec(sql, (email,), fetch='one')
        if u and check_password_hash(u['password_hash'], senha):
            session['user_email'] = u['email']
            session['user_nome'] = u['nome'] or u['email'].split('@')[0]
            return redirect(url_for('dashboard'))
        erro = 'Email ou senha incorretos.'
    return render_template('login.html', modo='login', erro=erro)

@app.route('/cadastro', methods=['GET', 'POST'])
@_rate_limit('5 per minute')
def cadastro():
    if session.get('user_email'):
        return redirect(url_for('dashboard'))
    erro = None
    if request.method == 'POST':
        nome  = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '').strip()
        conf  = request.form.get('confirmar', '').strip()
        if not email or not senha:
            erro = 'Preencha email e senha.'
        elif senha != conf:
            erro = 'As senhas não coincidem.'
        elif len(senha) < 8:
            erro = 'A senha deve ter pelo menos 8 caracteres.'
        else:
            sql_check = 'SELECT id FROM users WHERE email=%s' if USE_PG else 'SELECT id FROM users WHERE email=?'
            if db_exec(sql_check, (email,), fetch='one'):
                erro = 'Este email já está cadastrado.'
            else:
                ph = generate_password_hash(senha)
                sql_ins = 'INSERT INTO users(email,password_hash,nome,criado_em) VALUES(%s,%s,%s,%s)' if USE_PG else \
                          'INSERT INTO users(email,password_hash,nome,criado_em) VALUES(?,?,?,?)'
                from datetime import timedelta
                trial_end = (datetime.now(BRASILIA) + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
                db_exec(sql_ins, (email, ph, nome, now_str()))
                sql_trial = 'UPDATE users SET trial_ends_at=%s, subscription_status=%s WHERE email=%s' if USE_PG else \
                            'UPDATE users SET trial_ends_at=?, subscription_status=? WHERE email=?'
                db_exec(sql_trial, (trial_end, 'trial', email))
                save_config({'email_remetente': email})
                session['user_email'] = email
                session['user_nome']  = nome or email.split('@')[0]
                session['user_foto']  = ''
                notificar_admin_novo_usuario(nome or email, email, 'email')
                return redirect(url_for('dashboard'))
    return render_template('login.html', modo='cadastro', erro=erro)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── helpers OAuth ─────────────────────────────────────────────────────────────

def notificar_admin_novo_usuario(nome, email, provider):
    """Notifica o admin quando um novo usuário se cadastra."""
    cfg = get_config()
    hora = datetime.now(BRASILIA).strftime('%d/%m/%Y às %H:%M')
    txt = (f"🆕 Novo cadastro no ABRIU!\n\n"
           f"👤 Nome: {nome}\n"
           f"📧 Email: {email}\n"
           f"🔑 Via: {provider.capitalize()}\n"
           f"🕐 {hora}")
    threading.Thread(target=lambda: notificar(cfg, txt), daemon=True).start()

def processar_login_social(email, nome, provider, provider_id, foto=None):
    """Loga ou cria usuário via OAuth e retorna redirect."""
    if not email:
        return redirect(url_for('login') + '?erro=sem_email')
    sql = 'SELECT * FROM users WHERE email=%s' if USE_PG else 'SELECT * FROM users WHERE email=?'
    u = db_exec(sql, (email.lower(),), fetch='one')
    novo = False
    if not u:
        # Novo usuário — cria conta
        from datetime import timedelta
        trial_end = (datetime.now(BRASILIA) + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
        sql_ins = ('INSERT INTO users(email,nome,criado_em,oauth_provider,oauth_id,foto,trial_ends_at,subscription_status) '
                   'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)') if USE_PG else \
                  ('INSERT INTO users(email,nome,criado_em,oauth_provider,oauth_id,foto,trial_ends_at,subscription_status) '
                   'VALUES(?,?,?,?,?,?,?,?)')
        db_exec(sql_ins, (email.lower(), nome, now_str(), provider, provider_id, foto, trial_end, 'trial'))
        u = db_exec(sql, (email.lower(),), fetch='one')
        novo = True
    else:
        # Atualiza foto e provider se necessário
        sql_up = ('UPDATE users SET foto=%s, oauth_provider=%s WHERE email=%s') if USE_PG else \
                 ('UPDATE users SET foto=?, oauth_provider=? WHERE email=?')
        db_exec(sql_up, (foto, provider, email.lower()))
    session['user_email'] = u['email']
    session['user_nome']  = u['nome'] or u['email'].split('@')[0]
    session['user_foto']  = foto or ''
    if novo:
        notificar_admin_novo_usuario(nome, email, provider)
    return redirect(url_for('dashboard'))

# ── Gmail OAuth (para notificações — sem senha de app) ───────────────────────

@app.route('/auth/gmail')
@login_required
def auth_gmail():
    client_id = os.environ.get('GOOGLE_CLIENT_ID', '')
    if not client_id:
        return redirect(url_for('configuracoes') + '?erro=google_nao_configurado')
    base = get_base_url()
    state = secrets.token_urlsafe(24)
    session['gmail_oauth_state'] = state
    params = ('client_id=' + client_id +
              '&redirect_uri=' + requests.utils.quote(base + '/auth/gmail/callback') +
              '&response_type=code'
              '&scope=' + requests.utils.quote('https://www.googleapis.com/auth/gmail.send email profile') +
              '&access_type=offline'
              '&state=' + state +
              '&prompt=consent')
    return redirect('https://accounts.google.com/o/oauth2/v2/auth?' + params)

@app.route('/auth/gmail/callback')
@login_required
def auth_gmail_callback():
    code = request.args.get('code', '')
    if not code:
        return redirect(url_for('configuracoes') + '?gmail_erro=cancelado')
    # Anti-CSRF: o state retornado tem que bater com o que guardamos na sessão
    state_recebido = request.args.get('state', '')
    state_esperado = session.pop('gmail_oauth_state', None)
    if not state_esperado or not secrets.compare_digest(state_recebido, state_esperado):
        return redirect(url_for('configuracoes') + '?gmail_erro=state_invalido')
    base = get_base_url()
    try:
        tok = requests.post('https://oauth2.googleapis.com/token', data={
            'code':          code,
            'client_id':     os.environ.get('GOOGLE_CLIENT_ID', ''),
            'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
            'redirect_uri':  base + '/auth/gmail/callback',
            'grant_type':    'authorization_code',
        }, timeout=15).json()
        refresh_token = tok.get('refresh_token', '')
        access_token  = tok.get('access_token', '')
        if not refresh_token:
            return redirect(url_for('configuracoes') + '?gmail_erro=sem_refresh_token')
        # Pega o email da conta conectada
        info = requests.get('https://www.googleapis.com/oauth2/v3/userinfo',
            headers={'Authorization': 'Bearer ' + access_token}, timeout=15).json()
        gmail_email = info.get('email', session.get('user_email', ''))
        from flask import g
        save_user_config(g.current_user['id'], {'gmail_refresh_token': refresh_token, 'gmail_email': gmail_email})
    except Exception as e:
        return redirect(url_for('configuracoes') + '?gmail_erro=excecao')
    return redirect(url_for('configuracoes') + '?gmail_ok=1')

@app.route('/auth/gmail/disconnect', methods=['POST'])
@login_required
def auth_gmail_disconnect():
    from flask import g
    save_user_config(g.current_user['id'], {'gmail_refresh_token': '', 'gmail_email': ''})
    return jsonify({'ok': True})

@app.route('/gmail/status')
@login_required
def gmail_status():
    from flask import g
    cfg = get_user_config(g.current_user['id'])
    conectado = bool(cfg.get('gmail_refresh_token'))
    return jsonify({'conectado': conectado, 'email': cfg.get('gmail_email', '')})

def save_config(dados):
    for k, v in dados.items():
        if USE_PG:
            db_exec('INSERT INTO config(chave,valor) VALUES(%s,%s) ON CONFLICT(chave) DO UPDATE SET valor=EXCLUDED.valor', (k, v))
        else:
            db_exec('INSERT OR REPLACE INTO config(chave,valor) VALUES(?,?)', (k, v))

# ── config por usuário ────────────────────────────────────────────────────────
# Tabela criada em init_db — sem overhead lazy por request

def get_user_config(user_id):
    from flask import g
    cache_key = f'_ucfg_{user_id}'
    cached = getattr(g, cache_key, None)
    if cached is not None:
        return cached
    sql = 'SELECT chave, valor FROM user_config WHERE user_id=%s' if USE_PG else \
          'SELECT chave, valor FROM user_config WHERE user_id=?'
    rows = db_exec(sql, (user_id,), fetch='all') or []
    result = {r['chave']: r['valor'] for r in rows}
    try:
        setattr(g, cache_key, result)   # cache no contexto da requisição
    except RuntimeError:
        pass  # fora de contexto de request (ex: background thread)
    return result

def save_user_config(user_id, dados):
    for k, v in dados.items():
        if USE_PG:
            db_exec('INSERT INTO user_config(user_id,chave,valor) VALUES(%s,%s,%s) '
                    'ON CONFLICT(user_id,chave) DO UPDATE SET valor=EXCLUDED.valor', (user_id, k, v))
        else:
            db_exec('INSERT OR REPLACE INTO user_config(user_id,chave,valor) VALUES(?,?,?)', (user_id, k, v))

def user_config_completo(user_id):
    """True se o usuário já configurou pelo menos um canal de notificação."""
    cfg = get_user_config(user_id)
    return bool(
        (cfg.get('zapi_instance') and cfg.get('zapi_phone')) or
        cfg.get('whatsapp_numero') or
        cfg.get('telegram_chat_id') or
        cfg.get('gmail_refresh_token') or
        cfg.get('email_remetente')
    )

def extrair_valor_pdf(dados_bytes):
    """Extrai o valor total do PDF usando pdfplumber + IA (Claude) ou regex robusta."""
    try:
        import pdfplumber
        texto = ''
        with pdfplumber.open(io.BytesIO(dados_bytes)) as pdf:
            for page in pdf.pages:
                texto += (page.extract_text() or '') + '\n'

        if not texto.strip():
            return 0.0

        # ── Remove espaços entre dígitos (PDFs problemáticos: "1 47.269,58" → "147.269,58")
        def limpar_texto(t):
            for _ in range(4):
                t = re.sub(r'(\d) (\d)', r'\1\2', t)
            return t

        def parse_brl(s):
            """Converte string brasileira para float. Ex: '147.269,58' → 147269.58"""
            s = s.strip().replace(' ', '')
            if re.fullmatch(r'[\d.]+,\d{2}', s):
                return float(s.replace('.', '').replace(',', '.'))
            if re.fullmatch(r'\d+,\d{2}', s):
                return float(s.replace(',', '.'))
            return None

        # ── Tentar IA primeiro (Claude)
        anthropic_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if anthropic_key:
            try:
                import anthropic
                # timeout curto + sem retries: o default do SDK é 10min, o que penduraria o worker
                client = anthropic.Anthropic(api_key=anthropic_key, timeout=20.0, max_retries=0)
                msg = client.messages.create(
                    model='claude-3-haiku-20240307',
                    max_tokens=150,
                    messages=[{
                        'role': 'user',
                        'content': (
                            'Analise este orçamento e retorne APENAS o valor total final '
                            '(geralmente próximo a palavras como TOTAL, TOTAL GERAL, VALOR TOTAL, '
                            'TOTAL A PAGAR, TOTAL DO ORÇAMENTO).\n'
                            'Retorne SOMENTE o número no formato brasileiro com vírgula decimal. '
                            'Exemplo de resposta: 147269,58\n'
                            'Se não encontrar, retorne: 0\n\n'
                            f'Texto:\n{limpar_texto(texto)[:4000]}'
                        )
                    }]
                )
                raw = msg.content[0].text.strip().replace('R$', '').replace(' ', '')
                # Tenta interpretar como número brasileiro
                m = re.search(r'([\d]{1,3}(?:\.?\d{3})*,\d{2})', raw)
                if m:
                    v = parse_brl(m.group(1))
                    if v and v > 0:
                        return v
                # Fallback: remove tudo exceto dígitos e ponto
                raw2 = re.sub(r'[^\d.]', '', raw)
                if raw2 and raw2 != '0':
                    return float(raw2)
            except Exception:
                pass

        texto_limpo = limpar_texto(texto)
        linhas = texto_limpo.splitlines()

        def extrair_valor_linha(linha):
            """Extrai o maior valor BRL de uma linha."""
            nums = re.findall(r'R\$\s*([\d.]+,\d{2})', linha)
            if not nums:
                nums = re.findall(r'([\d]{1,3}(?:\.\d{3})+,\d{2})', linha)
            if not nums:
                nums = re.findall(r'(\d{3,},\d{2})', linha)
            vals = [v for v in [parse_brl(n) for n in nums] if v and v > 0]
            return max(vals) if vals else None

        # ── PRIORIDADE 1: última linha que contém "VALOR TOTAL" (regra do usuário)
        for linha in reversed(linhas):
            if re.search(r'VALOR\s+TOTAL', linha, re.IGNORECASE):
                v = extrair_valor_linha(linha)
                if v:
                    return v

        # ── PRIORIDADE 2: última linha com outras variantes de total
        for kw in [r'TOTAL\s+GERAL', r'TOTAL\s+A\s+PAGAR',
                   r'TOTAL\s+DO\s+OR[CÇ]AMENTO', r'TOTAL\s+FINAL',
                   r'GRAND\s+TOTAL', r'TOTAL']:
            for linha in reversed(linhas):
                if re.search(kw, linha, re.IGNORECASE):
                    v = extrair_valor_linha(linha)
                    if v:
                        return v

        # ── FALLBACK: maior valor monetário do documento inteiro
        all_nums = re.findall(r'R\$\s*([\d.]+,\d{2})', texto_limpo)
        if not all_nums:
            all_nums = re.findall(r'([\d]{1,3}(?:\.\d{3})+,\d{2})', texto_limpo)
        if not all_nums:
            all_nums = re.findall(r'(\d{3,},\d{2})', texto_limpo)
        vals = [v for v in [parse_brl(n) for n in all_nums] if v and v > 0]
        return max(vals) if vals else 0.0

    except Exception:
        return 0.0

def get_base_url():
    cfg = get_config()
    base = cfg.get('base_url', '').strip()
    if not base:
        base = request.host_url.rstrip('/')
    return base.rstrip('/')

# ── notificações ────────────────────────────────────────────────────────────

def notificar_whatsapp(numero, apikey, msg):
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={numero}&text={requests.utils.quote(msg)}&apikey={apikey}"
        r = requests.get(url, timeout=15)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def notificar_email(email, senha, html):
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = '👁 Cliente abriu seu orçamento!'
        msg['From'] = email
        msg['To'] = email
        msg.attach(MIMEText(html, 'html'))
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as s:
            s.starttls()
            s.login(email, senha)
            s.sendmail(email, email, msg.as_string())
        return True, 'ok'
    except BaseException as e:
        return False, str(e)

def notificar_email_gmail(refresh_token, sender_email, html, subject='👁 Notificação ABRIU'):
    """Envia email via Gmail API usando OAuth (sem precisar de senha de app)."""
    try:
        # 1. Troca refresh_token por access_token
        tok = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id':     os.environ.get('GOOGLE_CLIENT_ID', ''),
            'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
            'refresh_token': refresh_token,
            'grant_type':    'refresh_token',
        }, timeout=15).json()
        access_token = tok.get('access_token', '')
        if not access_token:
            return False, f"Sem access_token: {tok.get('error_description', tok)}"
        # 2. Monta mensagem MIME
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = sender_email
        msg.attach(MIMEText(html, 'html'))
        # 3. Codifica em base64url
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        # 4. Envia via Gmail API
        r = requests.post(
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
            json={'raw': raw},
            timeout=15
        )
        if r.status_code == 200:
            return True, 'ok'
        return False, r.text
    except Exception as e:
        return False, str(e)

def enviar_pdf_email_cliente(cfg, cliente_email, cliente_nome, titulo, pdf_bytes, filename, link, link_aceite=''):
    """Envia o PDF rastreado como anexo para o email do cliente."""
    assunto = f"Orçamento: {titulo}"
    botao_aceite = f"""
      <p style="color:#444;font-size:15px;margin-top:18px">Gostou? Você pode aceitar o orçamento com um clique:</p>
      <a href="{link_aceite}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:600;margin:8px 0">
        ✅ Aceitar orçamento
      </a>""" if link_aceite else ''
    corpo_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
      <h2 style="color:#0D1B2A">Olá, {cliente_nome}!</h2>
      <p style="color:#444;font-size:15px">
        Segue em anexo o orçamento <strong>{titulo}</strong> conforme solicitado.
      </p>
      <p style="color:#444;font-size:15px">
        Você também pode visualizá-lo online clicando no botão abaixo:
      </p>
      <a href="{link}" style="display:inline-block;background:#E8441A;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:600;margin:8px 0">
        Ver orçamento online
      </a>
      {botao_aceite}
      <p style="color:#888;font-size:13px;margin-top:24px">
        Qualquer dúvida estou à disposição. Obrigado!
      </p>
    </div>"""

    def _build_msg(from_addr):
        msg = MIMEMultipart('mixed')
        msg['Subject'] = assunto
        msg['From']    = from_addr
        msg['To']      = cliente_email
        msg.attach(MIMEText(corpo_html, 'html'))
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=filename)
        msg.attach(part)
        return msg

    if cfg.get('gmail_refresh_token') and cfg.get('gmail_email'):
        try:
            tok = requests.post('https://oauth2.googleapis.com/token', data={
                'client_id':     os.environ.get('GOOGLE_CLIENT_ID', ''),
                'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
                'refresh_token': cfg['gmail_refresh_token'],
                'grant_type':    'refresh_token',
            }, timeout=15).json()
            access_token = tok.get('access_token', '')
            if not access_token:
                return False, f"Sem access_token: {tok.get('error_description', tok)}"
            msg = _build_msg(cfg['gmail_email'])
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
            r = requests.post(
                'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
                json={'raw': raw}, timeout=30)
            return (True, 'ok') if r.status_code == 200 else (False, r.text)
        except Exception as e:
            return False, str(e)
    elif cfg.get('email_remetente') and cfg.get('email_senha_app'):
        try:
            msg = _build_msg(cfg['email_remetente'])
            with smtplib.SMTP('smtp.gmail.com', 587, timeout=15) as s:
                s.starttls()
                s.login(cfg['email_remetente'], cfg['email_senha_app'])
                s.sendmail(cfg['email_remetente'], cliente_email, msg.as_string())
            return True, 'ok'
        except Exception as e:
            return False, str(e)
    return False, 'Email não configurado'


def notificar_telegram(token, chat_id, msg):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={'chat_id': chat_id, 'text': msg}, timeout=15)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def notificar_zapi(instance, token, client_token, phone, msg):
    try:
        url = f"https://api.z-api.io/instances/{instance}/token/{token}/send-text"
        headers = {'Content-Type': 'application/json', 'Client-Token': client_token}
        r = requests.post(url, json={'phone': phone, 'message': msg}, headers=headers, timeout=15)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def notificar_zapi_documento(instance, token, client_token, phone, pdf_bytes, filename, caption=''):
    """Envia arquivo PDF diretamente via WhatsApp usando Z-API (com tracker embutido)."""
    try:
        import base64
        url = f"https://api.z-api.io/instances/{instance}/token/{token}/send-document"
        headers = {'Content-Type': 'application/json', 'Client-Token': client_token}
        b64 = base64.b64encode(pdf_bytes).decode('utf-8')
        payload = {
            'phone': phone,
            'document': f'data:application/pdf;base64,{b64}',
            'fileName': filename,
            'caption': caption,
        }
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def notificar(cfg, txt, html=None):
    def _check(canal, resultado):
        """Loga quando um canal de notificação falha — evita churn silencioso."""
        try:
            ok, resp = resultado
            if not ok:
                log.warning('notificar: canal %s falhou: %s', canal, str(resp)[:200])
        except Exception:
            pass
    def _enviar():
        enviou = False
        if cfg.get('whatsapp_numero') and cfg.get('whatsapp_apikey'):
            _check('whatsapp', notificar_whatsapp(cfg['whatsapp_numero'], cfg['whatsapp_apikey'], txt)); enviou = True
        # Gmail OAuth tem prioridade; cai no SMTP só se não tiver refresh_token
        if cfg.get('gmail_refresh_token') and cfg.get('gmail_email'):
            _check('gmail', notificar_email_gmail(cfg['gmail_refresh_token'], cfg['gmail_email'], html or f'<p>{txt}</p>')); enviou = True
        elif cfg.get('email_remetente') and cfg.get('email_senha_app'):
            _check('email', notificar_email(cfg['email_remetente'], cfg['email_senha_app'], html or f'<p>{txt}</p>')); enviou = True
        if cfg.get('telegram_token') and cfg.get('telegram_chat_id'):
            _check('telegram', notificar_telegram(cfg['telegram_token'], cfg['telegram_chat_id'], txt)); enviou = True
        if cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token') and cfg.get('zapi_phone'):
            _check('zapi', notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'], cfg['zapi_phone'], txt)); enviou = True
        if not enviou:
            log.warning('notificar: nenhum canal configurado — usuário não recebeu alerta')
    threading.Thread(target=_enviar, daemon=True).start()

# ── helpers ──────────────────────────────────────────────────────────────────

def calcular_total(itens):
    total = 0
    for etapa in itens:
        for item in etapa.get('itens', []):
            try: total += float(item.get('quantidade', 0)) * float(item.get('preco', 0))
            except: pass
    return total

def now_str():
    return datetime.now(BRASILIA).strftime('%Y-%m-%d %H:%M:%S')

# ── rastreio de acessos ──────────────────────────────────────────────────────

def detectar_dispositivo(ua):
    ua = (ua or '').lower()
    if 'ipad' in ua or 'tablet' in ua: return 'tablet'
    if 'mobile' in ua or 'android' in ua or 'iphone' in ua: return 'mobile'
    return 'desktop'

def detectar_modelo(ua):
    """Extrai o modelo real do dispositivo a partir do User-Agent."""
    import re
    ua_orig = ua or ''
    ua_low  = ua_orig.lower()

    # ── iOS / Apple ───────────────────────────────────────────────
    if 'iphone' in ua_low:
        m = re.search(r'CPU iPhone OS ([\d_]+)', ua_orig)
        if m:
            ver = m.group(1).replace('_', '.')
            v = int(ver.split('.')[0])
            # Mapeamento iOS → modelo aproximado (lançamento)
            ios_map = {18:'iPhone 16', 17:'iPhone 15', 16:'iPhone 14',
                       15:'iPhone 13', 14:'iPhone 12', 13:'iPhone 11',
                       12:'iPhone XR/XS', 11:'iPhone X'}
            modelo = ios_map.get(v, f'iPhone (iOS {ver})')
            return modelo
        return 'iPhone'
    if 'ipad' in ua_low:
        return 'iPad'
    if 'macintosh' in ua_low or 'mac os x' in ua_low:
        return 'Mac'

    # ── Android — modelo específico ───────────────────────────────
    if 'android' in ua_low:
        # Padrão: (Linux; Android X.X; MODELO Build/...)
        m = re.search(r'Android[\s/][\d.]+;\s*([^)]+?)\s*(?:Build|;|\))', ua_orig)
        if m:
            modelo = m.group(1).strip()
            # Remove sufixos desnecessários
            modelo = re.sub(r'\s*(Build|wv|LTE|4G|5G).*', '', modelo, flags=re.IGNORECASE).strip()
            if modelo and len(modelo) > 1:
                # Melhora nomes de fabricantes conhecidos
                fab_map = [
                    ('SM-S9', 'Samsung Galaxy S23'),('SM-S8', 'Samsung Galaxy S22'),
                    ('SM-S7', 'Samsung Galaxy S21'),('SM-S6', 'Samsung Galaxy S20'),
                    ('SM-A5', 'Samsung Galaxy A5x'),('SM-A3', 'Samsung Galaxy A3x'),
                    ('SM-G9', 'Samsung Galaxy S10'),('SM-N', 'Samsung Galaxy Note'),
                    ('SM-', 'Samsung Galaxy'),
                    ('Pixel 8', 'Google Pixel 8'),('Pixel 7', 'Google Pixel 7'),
                    ('Pixel 6', 'Google Pixel 6'),('Pixel 5', 'Google Pixel 5'),
                    ('Redmi', 'Xiaomi Redmi'),('POCO', 'Xiaomi POCO'),
                    ('Mi ', 'Xiaomi Mi'),
                    ('CPH', 'OPPO'),('RMX', 'Realme'),
                    ('LM-', 'LG'),('V60', 'LG V60'),
                    ('Moto G', 'Motorola Moto G'),('Moto E', 'Motorola Moto E'),
                    ('XT', 'Motorola'),
                ]
                for prefix, nome in fab_map:
                    if modelo.startswith(prefix):
                        if nome.endswith(prefix.rstrip()):
                            return nome
                        if modelo == prefix.strip():
                            return nome
                        # Se o modelo tem mais info, usa o modelo completo com fabricante
                        fab = nome.split()[0]
                        return f'{fab} {modelo}' if not modelo.lower().startswith(fab.lower()) else modelo
                return modelo
        return 'Android'

    # ── Windows ───────────────────────────────────────────────────
    if 'windows nt' in ua_low:
        nt_map = {'10.0':'Windows 10/11','6.3':'Windows 8.1','6.2':'Windows 8','6.1':'Windows 7'}
        m = re.search(r'Windows NT ([\d.]+)', ua_orig)
        ver = m.group(1) if m else ''
        return nt_map.get(ver, f'Windows {ver}')

    # ── Linux Desktop ─────────────────────────────────────────────
    if 'linux' in ua_low:
        return 'Linux'

    return 'Dispositivo desconhecido'

def hostname_ip(ip):
    """Reverse DNS — pode revelar nome do host em redes corporativas."""
    try:
        import socket
        if not ip or ip in ('127.0.0.1','::1') or ip.startswith(('192.168.','10.','172.')):
            return None
        h = socket.gethostbyaddr(ip)[0]
        # Filtra hostnames genéricos de CGNAT/ISP (não são úteis)
        genericos = ['static','dynamic','pool','dhcp','cgnat','broadband',
                     'cable','dsl','fiber','mobile','gprs','3g','4g','5g',
                     'users','customer','client','host','node','rev']
        h_low = h.lower()
        if any(g in h_low for g in genericos):
            return None
        return h
    except Exception:
        return None

def geolocate_ip(ip):
    try:
        import urllib.request, json as _json
        if not ip or ip in ('127.0.0.1','::1') or ip.startswith(('192.168.','10.','172.')):
            return {'cidade':'Local','regiao':'','pais':'BR','operadora':'', 'hostname': None}
        with urllib.request.urlopen(
            f'http://ip-api.com/json/{ip}?fields=status,city,regionName,countryCode,isp,org,mobile', timeout=3
        ) as r:
            d = _json.loads(r.read())
        if d.get('status') == 'success':
            return {
                'cidade':    d.get('city',''),
                'regiao':    d.get('regionName',''),
                'pais':      d.get('countryCode',''),
                'operadora': d.get('isp','') or d.get('org',''),
                'mobile':    d.get('mobile', False),
                'hostname':  hostname_ip(ip),
            }
    except Exception:
        pass
    return {'cidade':'','regiao':'','pais':'','operadora':'','mobile':False,'hostname':None}

def registrar_acesso(pdf_id, token, ip, ua, fonte):
    """Registra acesso com geo + modelo do dispositivo + hostname em background thread."""
    def _worker():
        try:
            geo    = geolocate_ip(ip)  # inclui hostname_ip interno
            disp   = detectar_dispositivo(ua)
            modelo = detectar_modelo(ua)
            # sistema = modelo (já bem descritivo), hostname sobrescreve se disponível
            nome_disp = geo.get('hostname') or modelo
            if USE_PG:
                db_exec('INSERT INTO pdf_acessos(pdf_id,token,ip,user_agent,dispositivo,sistema,cidade,regiao,pais,operadora,fonte,criado_em) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (pdf_id, token, ip, (ua or '')[:500], disp, nome_disp, geo['cidade'], geo['regiao'], geo['pais'], geo['operadora'], fonte, now_str()))
            else:
                db_exec('INSERT INTO pdf_acessos(pdf_id,token,ip,user_agent,dispositivo,sistema,cidade,regiao,pais,operadora,fonte,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (pdf_id, token, ip, (ua or '')[:500], disp, nome_disp, geo['cidade'], geo['regiao'], geo['pais'], geo['operadora'], fonte, now_str()))
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()

# ── rotas ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if session.get('user_email'):
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

@app.route('/dashboard')
@login_required
def dashboard():
    from flask import g
    u = g.current_user
    uid = u['id']
    sql = 'SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas,valor,decisao FROM pdfs WHERE user_id=%s ORDER BY criado_em DESC' if USE_PG else \
          'SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas,valor,decisao FROM pdfs WHERE user_id=? ORDER BY criado_em DESC'
    pdfs = db_exec(sql, (uid,), fetch='all') or []
    for p in pdfs:
        p['valor'] = float(p['valor'] or 0)
    dias = dias_trial_restantes(u)
    return render_template('dashboard.html', pdfs=pdfs, dias_trial=dias,
                           subscription_status=u.get('subscription_status') if u else None)

@app.route('/criar', methods=['GET', 'POST'])
@login_required
def criar():
    from flask import g
    uid = g.current_user['id']
    if request.method == 'POST':
        d = request.get_json()
        token = str(uuid.uuid4())[:12].strip('-')
        if USE_PG:
            db_exec('INSERT INTO orcamentos(token,cliente_nome,cliente_telefone,titulo,itens,observacoes,prazo,forma_pagamento,validade,status,criado_em,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (token, d['cliente_nome'], d.get('cliente_telefone',''), d['titulo'],
                 json.dumps(d['itens'],ensure_ascii=False), d.get('observacoes',''),
                 d.get('prazo',''), d.get('forma_pagamento',''), d.get('validade',''), 'rascunho', now_str(), uid))
        else:
            db_exec('INSERT INTO orcamentos(token,cliente_nome,cliente_telefone,titulo,itens,observacoes,prazo,forma_pagamento,validade,status,criado_em,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (token, d['cliente_nome'], d.get('cliente_telefone',''), d['titulo'],
                 json.dumps(d['itens'],ensure_ascii=False), d.get('observacoes',''),
                 d.get('prazo',''), d.get('forma_pagamento',''), d.get('validade',''), 'rascunho', now_str(), uid))
        return jsonify({'ok': True, 'token': token})
    return render_template('criar.html', orcamento=None)

@app.route('/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar(id):
    from flask import g
    uid = g.current_user['id']
    if request.method == 'POST':
        d = request.get_json()
        p = (d['cliente_nome'], d.get('cliente_telefone',''), d['titulo'],
             json.dumps(d['itens'],ensure_ascii=False), d.get('observacoes',''),
             d.get('prazo',''), d.get('forma_pagamento',''), d.get('validade',''), id, uid)
        sql = 'UPDATE orcamentos SET cliente_nome=%s,cliente_telefone=%s,titulo=%s,itens=%s,observacoes=%s,prazo=%s,forma_pagamento=%s,validade=%s WHERE id=%s AND user_id=%s' if USE_PG else \
              'UPDATE orcamentos SET cliente_nome=?,cliente_telefone=?,titulo=?,itens=?,observacoes=?,prazo=?,forma_pagamento=?,validade=? WHERE id=? AND user_id=?'
        db_exec(sql, p)
        return jsonify({'ok': True})
    sql = 'SELECT * FROM orcamentos WHERE id=%s AND user_id=%s' if USE_PG else 'SELECT * FROM orcamentos WHERE id=? AND user_id=?'
    o = db_exec(sql, (id, uid), fetch='one')
    if not o: return redirect(url_for('dashboard'))
    o['itens'] = json.loads(o['itens'])
    return render_template('criar.html', orcamento=o)

@app.route('/deletar/<int:id>', methods=['POST'])
@login_required
def deletar(id):
    from flask import g
    uid = g.current_user['id']
    sql = 'DELETE FROM orcamentos WHERE id=%s AND user_id=%s' if USE_PG else 'DELETE FROM orcamentos WHERE id=? AND user_id=?'
    db_exec(sql, (id, uid))
    return redirect(url_for('dashboard'))

@app.route('/ver/<token>')
def ver(token):
    sql = 'SELECT * FROM orcamentos WHERE token=%s' if USE_PG else 'SELECT * FROM orcamentos WHERE token=?'
    o = db_exec(sql, (token,), fetch='one')
    if not o: return 'Orçamento não encontrado.', 404
    cfg = get_config()
    if o['status'] == 'enviado':
        sql2 = "UPDATE orcamentos SET status='aberto',aberto_em=%s WHERE token=%s" if USE_PG else \
               "UPDATE orcamentos SET status='aberto',aberto_em=? WHERE token=?"
        db_exec(sql2, (now_str(), token))
        hora = datetime.now(BRASILIA).strftime('%H:%M')
        txt = f"✅ {o['cliente_nome']} abriu o orçamento {o['titulo']} às {hora}!"
        html = f"""<div style="font-family:sans-serif;padding:2rem;background:#f0fdf4;border-radius:12px">
            <h2 style="color:#16a34a">👁 Orçamento Visualizado!</h2>
            <p><strong>{o['cliente_nome']}</strong> abriu <strong>{o['titulo']}</strong> às <strong>{hora}</strong>.</p></div>"""
        threading.Thread(target=lambda: notificar(cfg, txt, html), daemon=True).start()
        o = db_exec(sql, (token,), fetch='one')
    o['itens'] = json.loads(o['itens'])
    o['total'] = calcular_total(o['itens'])
    o['empresa_nome'] = cfg.get('empresa_nome', '')
    return render_template('orcamento.html', o=o)

@app.route('/marcar_enviado/<int:id>', methods=['POST'])
@login_required
def marcar_enviado(id):
    from flask import g
    uid = g.current_user['id']
    sql = "UPDATE orcamentos SET status='enviado' WHERE id=%s AND user_id=%s" if USE_PG else \
          "UPDATE orcamentos SET status='enviado' WHERE id=? AND user_id=?"
    db_exec(sql, (id, uid))
    return jsonify({'ok': True})

@app.route('/atualizar_status/<int:id>', methods=['POST'])
@login_required
def atualizar_status(id):
    from flask import g
    uid = g.current_user['id']
    d = request.get_json()
    status = d.get('status','')
    if status not in ['fechou','negociando','perdido','enviado','aberto']:
        return jsonify({'ok': False})
    sql = 'UPDATE orcamentos SET status=%s WHERE id=%s AND user_id=%s' if USE_PG else 'UPDATE orcamentos SET status=? WHERE id=? AND user_id=?'
    db_exec(sql, (status, id, uid))
    return jsonify({'ok': True})

@app.route('/link/<token>')
@login_required
def gerar_link(token):
    return jsonify({'link': f"{get_base_url()}/ver/{token}"})

# ── Aceite do orçamento (fecha a venda no próprio link do cliente) ────────────

def _buscar_por_token(token):
    """Retorna (tabela, registro) procurando o token em pdfs e depois em orcamentos."""
    cols = 'id, token, cliente_nome, titulo, user_id, decisao'
    for tabela in ('pdfs', 'orcamentos'):
        sql = f'SELECT {cols} FROM {tabela} WHERE token=%s' if USE_PG else \
              f'SELECT {cols} FROM {tabela} WHERE token=?'
        r = db_exec(sql, (token,), fetch='one')
        if r:
            return tabela, r
    return None, None

@app.route('/aceitar/<token>', methods=['GET', 'POST'])
@csrf_exempt
def aceitar(token):
    """Página pública onde o cliente aceita ou recusa o orçamento."""
    # tabela vem de um conjunto fixo ({'pdfs','orcamentos'}) — nunca de input do usuário
    tabela, reg = _buscar_por_token(token)
    if not reg:
        return render_template('aceite.html', titulo='', cliente='', token=token,
                               decisao='inexistente'), 404
    ja = (reg.get('decisao') or '')
    if request.method == 'POST' and not ja:
        decisao = request.form.get('decisao', 'aceito')
        if decisao not in ('aceito', 'recusado'):
            decisao = 'aceito'
        sql = f'UPDATE {tabela} SET decisao=%s, decisao_em=%s WHERE token=%s' if USE_PG else \
              f'UPDATE {tabela} SET decisao=?, decisao_em=? WHERE token=?'
        db_exec(sql, (decisao, now_str(), token))
        ja = decisao
        owner = reg.get('user_id')
        if owner:
            hora = datetime.now(BRASILIA).strftime('%H:%M')
            if decisao == 'aceito':
                txt  = f"🎉 {reg['cliente_nome']} ACEITOU o orçamento '{reg['titulo']}' às {hora}!"
                html = (f'<div style="font-family:sans-serif;padding:2rem;background:#f0fdf4;border-radius:12px">'
                        f'<h2 style="color:#16a34a">🎉 Orçamento Aceito!</h2>'
                        f'<p><strong>{reg["cliente_nome"]}</strong> aceitou <strong>{reg["titulo"]}</strong>'
                        f' às <strong>{hora}</strong>. Hora de combinar os próximos passos!</p></div>')
            else:
                txt  = f"🙁 {reg['cliente_nome']} recusou o orçamento '{reg['titulo']}' às {hora}."
                html = (f'<div style="font-family:sans-serif;padding:2rem;background:#fef2f2;border-radius:12px">'
                        f'<h2 style="color:#dc2626">Orçamento recusado</h2>'
                        f'<p><strong>{reg["cliente_nome"]}</strong> recusou <strong>{reg["titulo"]}</strong>'
                        f' às <strong>{hora}</strong>.</p></div>')
            cfg_u = get_user_config(owner)
            sse_push(owner, 'decisao', {'nome': reg['cliente_nome'], 'titulo': reg['titulo'], 'decisao': decisao})
            send_web_push(owner, txt, reg['titulo'])
            threading.Thread(target=lambda: notificar(cfg_u, txt, html), daemon=True).start()
    return render_template('aceite.html', titulo=reg['titulo'], cliente=reg['cliente_nome'],
                           token=token, decisao=ja)

# ── PDFs ─────────────────────────────────────────────────────────────────────

@app.route('/pdfs')
@login_required
def pdfs():
    from flask import g
    uid = g.current_user['id']
    sql = 'SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas FROM pdfs WHERE user_id=%s ORDER BY criado_em DESC' if USE_PG else \
          'SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas FROM pdfs WHERE user_id=? ORDER BY criado_em DESC'
    rows = db_exec(sql, (uid,), fetch='all') or []
    return render_template('pdfs.html', pdfs=rows)

@app.route('/upload_pdf', methods=['POST'])
@_rate_limit('20 per hour')
@login_required
def upload_pdf():
    if 'pdf' not in request.files:
        return jsonify({'ok': False, 'erro': 'Nenhum arquivo enviado'})
    f = request.files['pdf']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'erro': 'Apenas PDFs são aceitos'})
    dados_originais = f.read()
    if len(dados_originais) < 4 or dados_originais[:4] != b'%PDF':
        return jsonify({'ok': False, 'erro': 'Arquivo inválido — não é um PDF real'})
    token = str(uuid.uuid4()).replace('-','')[:16]
    filename = secure_filename(f.filename)
    valor_manual = request.form.get('valor', '').replace('R$','').replace('.','').replace(',','.').strip()
    try: valor = float(valor_manual) if valor_manual else None
    except: valor = None
    # Valor: usa o manual se informado; senão extrai em BACKGROUND (não trava o upload/site)
    valor = valor or 0.0
    extrair_valor_async = (valor == 0.0)
    # Comprime o PDF e emite rastreador embutido no arquivo
    dados = comprimir_pdf(dados_originais)
    tracking_url = f"{get_base_url()}/track/{token}"
    dados = embed_tracker_pdf(dados, tracking_url)
    from flask import g
    uid = g.current_user['id']
    arquivo_key = None
    arquivo_db  = None
    if r2_configured():
        arquivo_key = f'pdfs/{uid}/{token}.pdf'
        r2_upload(arquivo_key, dados)
    else:
        arquivo_db = dados
    cliente_email = request.form.get('cliente_email', '').strip()
    if USE_PG:
        import psycopg2
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,cliente_email,titulo,arquivo,arquivo_key,filename,status,criado_em,valor,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             cliente_email, request.form.get('titulo', filename),
             psycopg2.Binary(arquivo_db) if arquivo_db else None,
             arquivo_key, filename, 'enviado', now_str(), valor, uid))
    else:
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,cliente_email,titulo,arquivo,arquivo_key,filename,status,criado_em,valor,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             cliente_email, request.form.get('titulo', filename), arquivo_db,
             arquivo_key, filename, 'enviado', now_str(), valor, uid))
    # Extração de valor por IA roda em background — a chamada de rede não bloqueia a resposta
    if extrair_valor_async:
        raw_pdf = bytes(dados_originais)
        def _extrair_valor(pdf_raw=raw_pdf, tok=token, owner=uid):
            try:
                v = extrair_valor_pdf(pdf_raw) or 0.0
                if v and v > 0:
                    sqlv = 'UPDATE pdfs SET valor=%s WHERE token=%s' if USE_PG else \
                           'UPDATE pdfs SET valor=? WHERE token=?'
                    db_exec(sqlv, (v, tok))
                    sse_push(owner, 'valor', {'token': tok, 'valor': v})
            except Exception:
                pass
        threading.Thread(target=_extrair_valor, daemon=True).start()

    link = f"{get_base_url()}/pdf/{token}"
    link_aceite = f"{get_base_url()}/aceitar/{token}"
    cliente_nome = request.form.get('cliente_nome', 'Cliente')
    cliente_tel = request.form.get('cliente_telefone', '').strip()
    titulo = request.form.get('titulo', filename)

    cfg = get_user_config(uid)
    pdf_snapshot = bytes(dados)
    fname = filename or 'orcamento.pdf'

    # Enviar arquivo PDF + link via WhatsApp (rastreador embutido)
    if cliente_tel and cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token'):
        phone = '55' + cliente_tel if not cliente_tel.startswith('55') else cliente_tel
        caption = f"Olá, {cliente_nome}! 👋\n\nSegue o orçamento *{titulo}* conforme solicitado.\n\nQualquer dúvida estou à disposição!"
        link_msg = f"🔗 Para visualizar online: {link}\n\n✅ Aceitar o orçamento: {link_aceite}"
        def _enviar_wpp(pdf=pdf_snapshot, fn=fname, cap=caption, lm=link_msg):
            try:
                notificar_zapi_documento(
                    cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                    phone, pdf, fn, cap)
                import time; time.sleep(1)
                notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                               phone, lm)
            except Exception:
                pass
        threading.Thread(target=_enviar_wpp, daemon=True).start()

    # Enviar arquivo PDF + link via Email (rastreador embutido)
    if cliente_email:
        def _enviar_email(pdf=pdf_snapshot, fn=fname):
            try:
                enviar_pdf_email_cliente(cfg, cliente_email, cliente_nome, titulo, pdf, fn, link, link_aceite)
            except Exception:
                pass
        threading.Thread(target=_enviar_email, daemon=True).start()

    row_id = db_exec('SELECT id FROM pdfs WHERE token=%s' if USE_PG else 'SELECT id FROM pdfs WHERE token=?', (token,), fetch='one')['id']
    return jsonify({'ok': True, 'token': token, 'link': link, 'id': row_id, 'valor': valor, 'titulo': request.form.get('titulo', filename), 'tel': cliente_tel})

def comprimir_pdf(dados_bytes):
    """Comprime o PDF removendo objetos duplicados e comprimindo streams."""
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(dados_bytes))
        writer = PdfWriter()
        for page in reader.pages:
            page.compress_content_streams()
            writer.add_page(page)
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        out = io.BytesIO()
        writer.write(out)
        compressed = out.getvalue()
        return compressed if len(compressed) < len(dados_bytes) else dados_bytes
    except Exception:
        return dados_bytes

def embed_tracker_pdf(dados_bytes, tracking_url):
    """Injeta rastreador no PDF via JS (Acrobat/Foxit) e OpenAction URI (Chrome, Edge)."""
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import (
            DictionaryObject, NameObject, ArrayObject, create_string_object
        )
        reader = PdfReader(io.BytesIO(dados_bytes))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        # Método 1: JavaScript (Acrobat, Foxit — silencioso)
        js = (
            f'try{{'
            f'this.submitForm({{cURL:"{tracking_url}",cSubmitAs:"HTML",bEmpty:true}});'
            f'}}catch(e){{'
            f'try{{app.launchURL("{tracking_url}",true);}}catch(e2){{}}'
            f'}}'
        )
        writer.add_js(js)

        # Método 2: OpenAction URI (Chrome PDF viewer, Edge — silencioso na maioria)
        uri_action = DictionaryObject({
            NameObject('/Type'): NameObject('/Action'),
            NameObject('/S'):    NameObject('/URI'),
            NameObject('/URI'):  create_string_object(tracking_url),
        })
        writer._root_object[NameObject('/OpenAction')] = writer._add_object(uri_action)

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
        return dados_bytes

PRAZO_HORAS = 48  # link expira após 48h

@app.route('/track/<token>', methods=['GET', 'POST'])
def track_pdf(token):
    """Recebe ping do rastreador embutido no PDF baixado/compartilhado."""
    sql = ('SELECT id,token,cliente_nome,titulo,filename,status,aberturas,user_id FROM pdfs WHERE token=%s'
           if USE_PG else
           'SELECT id,token,cliente_nome,titulo,filename,status,aberturas,user_id FROM pdfs WHERE token=?')
    p = db_exec(sql, (token,), fetch='one')
    if not p:
        return '', 204
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    ua = request.headers.get('User-Agent', '')
    registrar_acesso(p['id'], token, ip, ua, 'arquivo')
    aberturas = (p['aberturas'] or 0) + 1
    primeira  = p['status'] == 'enviado'
    if USE_PG:
        db_exec("UPDATE pdfs SET aberturas=%s,aberto_em=%s,status=%s WHERE token=%s",
            (aberturas, now_str(), 'aberto', token))
    else:
        db_exec("UPDATE pdfs SET aberturas=?,aberto_em=?,status=? WHERE token=?",
            (aberturas, now_str(), 'aberto', token))
    agora   = datetime.now(BRASILIA)
    hora    = agora.strftime('%H:%M')
    owner   = p.get('user_id')
    cfg     = get_user_config(owner) if owner else {}
    if primeira:
        txt = f"👁 {p['cliente_nome']} abriu '{p['titulo']}' (arquivo baixado) pela 1ª vez às {hora}!"
        html_notif = f"""<div style="font-family:sans-serif;padding:2rem;background:#f0fdf4;border-radius:12px">
            <h2 style="color:#16a34a">👁 PDF Visualizado!</h2>
            <p><strong>{p['cliente_nome']}</strong> abriu <strong>{p['titulo']}</strong> às <strong>{hora}</strong>.<br>
            <small style="color:#64748b">Aberto via arquivo baixado/compartilhado</small></p></div>"""
    else:
        txt = f"🔄 {p['cliente_nome']} abriu novamente '{p['titulo']}' (arquivo) às {hora}. Total: {aberturas}x"
        html_notif = f"""<div style="font-family:sans-serif;padding:2rem;background:#fffbeb;border-radius:12px">
            <h2 style="color:#f59e0b">🔄 PDF Aberto Novamente</h2>
            <p><strong>{p['cliente_nome']}</strong> abriu <strong>{p['titulo']}</strong> às <strong>{hora}</strong>. Total: <strong>{aberturas}x</strong><br>
            <small style="color:#64748b">Aberto via arquivo baixado/compartilhado</small></p></div>"""
    if owner:
        sse_push(owner, 'abriu', {'nome': p['cliente_nome'], 'titulo': p['titulo'], 'hora': hora})
        send_web_push(owner, f"👁 {p['cliente_nome']} abriu o orçamento!", p['titulo'])
    threading.Thread(target=lambda: notificar(cfg, txt, html_notif), daemon=True).start()
    return '', 204

@app.route('/pdf/<token>')
def ver_pdf(token):
    """Serve o PDF imediatamente; registra abertura e notifica em background."""
    from datetime import timedelta

    # Busca metadados + blob em uma única query (evita 2ª roundtrip para PDFs antigos)
    try:
        sql = ('SELECT id,token,cliente_nome,titulo,filename,status,criado_em,'
               'aberturas,user_id,arquivo_key,arquivo FROM pdfs WHERE token=%s'
               if USE_PG else
               'SELECT id,token,cliente_nome,titulo,filename,status,criado_em,'
               'aberturas,user_id,arquivo_key,arquivo FROM pdfs WHERE token=?')
        p = db_exec(sql, (token,), fetch='one')
    except Exception:
        sql = ('SELECT id,token,cliente_nome,titulo,filename,status,criado_em,'
               'aberturas,user_id,arquivo FROM pdfs WHERE token=%s'
               if USE_PG else
               'SELECT id,token,cliente_nome,titulo,filename,status,criado_em,'
               'aberturas,user_id,arquivo FROM pdfs WHERE token=?')
        p = db_exec(sql, (token,), fetch='one')
    if not p:
        return 'Orçamento não encontrado.', 404

    # ── Verifica expiração ───────────────────────────────────────────────────
    try:
        criado = datetime.strptime(p['criado_em'][:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BRASILIA)
    except Exception:
        criado = datetime.now(BRASILIA)
    agora    = datetime.now(BRASILIA)
    expirado = agora > criado + timedelta(hours=PRAZO_HORAS)

    if expirado:
        owner = p.get('user_id')
        hora  = agora.strftime('%H:%M de %d/%m')
        txt   = (f"⏰ LINK EXPIRADO — {p['cliente_nome']} tentou abrir "
                 f"'{p['titulo']}' às {hora}, mas o link de 48h já venceu.")
        html_notif = (f'<div style="font-family:sans-serif;padding:2rem;background:#fef2f2;border-radius:12px">'
                      f'<h2 style="color:#dc2626">⏰ Link Expirado!</h2>'
                      f'<p><strong>{p["cliente_nome"]}</strong> tentou abrir <strong>{p["titulo"]}</strong>'
                      f' às <strong>{hora}</strong>, mas o link de 48h já venceu.</p></div>')
        cfg_u = get_user_config(owner) if owner else {}
        threading.Thread(target=lambda: notificar(cfg_u, txt, html_notif), daemon=True).start()
        return render_template('expirado.html', cliente=p['cliente_nome'],
                               titulo=p['titulo'], empresa=cfg_u.get('empresa_nome', 'a empresa')), 410

    # ── Prepara resposta antes de qualquer escrita ───────────────────────────
    filename    = (p['filename'] or 'orcamento.pdf').replace('"', '')
    arquivo_key = p.get('arquivo_key')
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    ua = request.headers.get('User-Agent', '')

    # Captura dados para o background ANTES de fazer qualquer I/O extra
    pdf_id    = p['id']
    owner     = p.get('user_id')
    aberturas = (p['aberturas'] or 0) + 1
    primeira  = p['status'] == 'enviado'
    hora      = agora.strftime('%H:%M')

    def _track():
        """Toda escrita + notificação roda em background — não bloqueia a entrega do PDF."""
        try:
            registrar_acesso(pdf_id, token, ip, ua, 'link')
            if USE_PG:
                db_exec("UPDATE pdfs SET aberturas=%s,aberto_em=%s,status=%s WHERE token=%s",
                        (aberturas, now_str(), 'aberto', token))
            else:
                db_exec("UPDATE pdfs SET aberturas=?,aberto_em=?,status=? WHERE token=?",
                        (aberturas, now_str(), 'aberto', token))
            cfg_u = get_user_config(owner) if owner else {}
            if primeira:
                txt = f"👁 {p['cliente_nome']} abriu '{p['titulo']}' pela 1ª vez às {hora}!"
                html_n = (f'<div style="font-family:sans-serif;padding:2rem;background:#f0fdf4;border-radius:12px">'
                          f'<h2 style="color:#16a34a">👁 PDF Visualizado!</h2>'
                          f'<p><strong>{p["cliente_nome"]}</strong> abriu <strong>{p["titulo"]}</strong>'
                          f' às <strong>{hora}</strong>.</p></div>')
            else:
                txt = f"🔄 {p['cliente_nome']} abriu novamente '{p['titulo']}' às {hora}. Total: {aberturas}x"
                html_n = (f'<div style="font-family:sans-serif;padding:2rem;background:#fffbeb;border-radius:12px">'
                          f'<h2 style="color:#f59e0b">🔄 PDF Aberto Novamente</h2>'
                          f'<p><strong>{p["cliente_nome"]}</strong> abriu <strong>{p["titulo"]}</strong>'
                          f' às <strong>{hora}</strong>. Total: <strong>{aberturas}x</strong></p></div>')
            if owner:
                sse_push(owner, 'abriu', {'nome': p['cliente_nome'], 'titulo': p['titulo'], 'hora': hora})
                send_web_push(owner, f"👁 {p['cliente_nome']} abriu o orçamento!", p['titulo'])
            notificar(cfg_u, txt, html_n)
        except Exception:
            pass

    threading.Thread(target=_track, daemon=True).start()

    # ── Serve o arquivo imediatamente ────────────────────────────────────────
    if arquivo_key:
        return redirect(r2_url(arquivo_key), 302)

    # PDFs antigos armazenados no banco
    arquivo = bytes(p['arquivo']) if p.get('arquivo') else b''
    return Response(arquivo, mimetype='application/pdf',
        headers={
            'Content-Disposition': f'inline; filename="{filename}"',
            'Cache-Control': 'public, max-age=3600, immutable',
        })

@app.route('/renovar_link/<int:id>', methods=['POST'])
@_rate_limit('10 per hour')
@login_required
def renovar_link(id):
    """Gera novo token, reseta prazo de 48h e envia o novo link para o cliente."""
    from flask import g
    uid = g.current_user['id']
    # Busca dados do PDF antes de renovar (filtra por user_id para segurança)
    sql_sel = 'SELECT cliente_nome, cliente_telefone, cliente_email, titulo FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else \
              'SELECT cliente_nome, cliente_telefone, titulo FROM pdfs WHERE id=? AND user_id=?'
    p = db_exec(sql_sel, (id, uid), fetch='one')
    if not p:
        return jsonify({'ok': False, 'erro': 'PDF não encontrado'})

    novo_token = str(uuid.uuid4()).replace('-','')[:16]

    # Busca metadados + chave R2 (ou blob legado)
    sql_arq = ('SELECT arquivo, arquivo_key, filename FROM pdfs WHERE id=%s'
               if USE_PG else
               'SELECT arquivo, arquivo_key, filename FROM pdfs WHERE id=?')
    arq_row = db_exec(sql_arq, (id,), fetch='one')
    old_key = (arq_row or {}).get('arquivo_key')
    fname   = ((arq_row or {}).get('filename') or 'orcamento.pdf')
    if old_key:
        arquivo_atual = r2_download(old_key)
    elif arq_row and arq_row.get('arquivo'):
        arquivo_atual = bytes(arq_row['arquivo'])
    else:
        arquivo_atual = b''

    novo_tracking_url = f"{get_base_url()}/track/{novo_token}"
    arquivo_novo = embed_tracker_pdf(arquivo_atual, novo_tracking_url)

    novo_key = None
    arquivo_novo_db = None
    if r2_configured():
        novo_key = f'pdfs/{uid}/{novo_token}.pdf'
        r2_upload(novo_key, arquivo_novo)
        if old_key and old_key != novo_key:
            r2_delete(old_key)
    else:
        arquivo_novo_db = arquivo_novo

    if USE_PG:
        import psycopg2
        db_exec('UPDATE pdfs SET token=%s, criado_em=%s, status=%s, aberturas=0, aberto_em=NULL, arquivo=%s, arquivo_key=%s WHERE id=%s',
                (novo_token, now_str(), 'enviado',
                 psycopg2.Binary(arquivo_novo_db) if arquivo_novo_db else None, novo_key, id))
    else:
        db_exec('UPDATE pdfs SET token=?, criado_em=?, status=?, aberturas=0, aberto_em=NULL, arquivo=?, arquivo_key=? WHERE id=?',
                (novo_token, now_str(), 'enviado', arquivo_novo_db, novo_key, id))

    link = f"{get_base_url()}/pdf/{novo_token}"
    link_aceite = f"{get_base_url()}/aceitar/{novo_token}"

    cfg          = get_user_config(uid)
    cliente_tel  = (p.get('cliente_telefone') or '').strip()
    cliente_nome = p.get('cliente_nome', 'Cliente')
    cliente_email_dest = (p.get('cliente_email') or '').strip()
    titulo_pdf   = p.get('titulo', fname)
    pdf_snap     = bytes(arquivo_novo)

    # Reenvia via WhatsApp com novo arquivo rastreado
    if cliente_tel and cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token'):
        phone   = '55' + cliente_tel if not cliente_tel.startswith('55') else cliente_tel
        caption = f"Olá, {cliente_nome}! 👋\n\nSegue o orçamento atualizado conforme solicitado.\n\nQualquer dúvida estou à disposição!"
        link_msg = f"🔗 Para visualizar online: {link}\n\n✅ Aceitar o orçamento: {link_aceite}"
        def _reenviar_wpp(pdf=pdf_snap, fn=fname, cap=caption, lm=link_msg):
            try:
                notificar_zapi_documento(
                    cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                    phone, pdf, fn, cap)
                import time; time.sleep(1)
                notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                               phone, lm)
            except Exception:
                pass
        threading.Thread(target=_reenviar_wpp, daemon=True).start()

    # Reenvia via email com novo arquivo rastreado
    if cliente_email_dest:
        def _reenviar_email(pdf=pdf_snap, fn=fname):
            try:
                enviar_pdf_email_cliente(cfg, cliente_email_dest, cliente_nome, titulo_pdf, pdf, fn, link, link_aceite)
            except Exception:
                pass
        threading.Thread(target=_reenviar_email, daemon=True).start()

    return jsonify({'ok': True, 'token': novo_token, 'link': link,
                    'enviado_wpp': bool(cliente_tel),
                    'enviado_email': bool(cliente_email_dest)})

@app.route('/atualizar_status_pdf/<int:id>', methods=['POST'])
@login_required
def atualizar_status_pdf(id):
    from flask import g
    uid = g.current_user['id']
    d = request.get_json()
    status = d.get('status','')
    if status not in ['enviado','aberto','fechou','negociando','perdido']:
        return jsonify({'ok': False})
    sql = 'UPDATE pdfs SET status=%s WHERE id=%s AND user_id=%s' if USE_PG else 'UPDATE pdfs SET status=? WHERE id=? AND user_id=?'
    db_exec(sql, (status, id, uid))
    return jsonify({'ok': True})

@app.route('/deletar_pdf/<int:id>', methods=['POST'])
@login_required
def deletar_pdf(id):
    from flask import g
    uid = g.current_user['id']
    row = db_exec('SELECT arquivo_key FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else
                  'SELECT arquivo_key FROM pdfs WHERE id=? AND user_id=?', (id, uid), fetch='one')
    if row and row.get('arquivo_key'):
        r2_delete(row['arquivo_key'])
    sql = 'DELETE FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else 'DELETE FROM pdfs WHERE id=? AND user_id=?'
    db_exec(sql, (id, uid))
    return redirect(url_for('dashboard'))

@app.route('/deletar_pdf_ajax/<int:id>', methods=['POST'])
@login_required
def deletar_pdf_ajax(id):
    from flask import g
    uid = g.current_user['id']
    row = db_exec('SELECT arquivo_key FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else
                  'SELECT arquivo_key FROM pdfs WHERE id=? AND user_id=?', (id, uid), fetch='one')
    if not row:
        return jsonify({'ok': False, 'erro': 'PDF não encontrado'}), 404
    if row.get('arquivo_key'):
        r2_delete(row['arquivo_key'])
    sql = 'DELETE FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else 'DELETE FROM pdfs WHERE id=? AND user_id=?'
    db_exec(sql, (id, uid))
    return jsonify({'ok': True})

@app.route('/acessos_pdf/<int:id>')
@login_required
def acessos_pdf(id):
    from flask import g
    uid = g.current_user['id']
    sql_chk = 'SELECT id FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else 'SELECT id FROM pdfs WHERE id=? AND user_id=?'
    if not db_exec(sql_chk, (id, uid), fetch='one'):
        return jsonify([])
    sql = 'SELECT ip,dispositivo,sistema,cidade,regiao,pais,operadora,fonte,criado_em FROM pdf_acessos WHERE pdf_id=%s ORDER BY criado_em ASC' if USE_PG else \
          'SELECT ip,dispositivo,sistema,cidade,regiao,pais,operadora,fonte,criado_em FROM pdf_acessos WHERE pdf_id=? ORDER BY criado_em ASC'
    rows = db_exec(sql, (id,), fetch='all') or []
    ips_vistos = []
    result = []
    for r in rows:
        ip = r['ip'] or ''
        novo_ip = ip not in ips_vistos and ip != ''
        if ip: ips_vistos.append(ip)
        result.append({
            'ip': ip,
            'dispositivo': r['dispositivo'],
            'sistema': r['sistema'],
            'cidade': r['cidade'],
            'regiao': r['regiao'],
            'pais': r['pais'],
            'operadora': r['operadora'],
            'fonte': r['fonte'],
            'criado_em': r['criado_em'],
            'novo_ip': novo_ip,
            'indice_ip': ips_vistos.index(ip) + 1 if ip in ips_vistos else 1
        })
    return jsonify(result)

@app.route('/reler_valor/<int:id>', methods=['POST'])
@login_required
def reler_valor(id):
    """Reextrai o valor total do PDF e atualiza no banco."""
    from flask import g
    uid = g.current_user['id']
    sql = ('SELECT arquivo, arquivo_key FROM pdfs WHERE id=%s AND user_id=%s'
           if USE_PG else 'SELECT arquivo, arquivo_key FROM pdfs WHERE id=? AND user_id=?')
    p = db_exec(sql, (id, uid), fetch='one')
    if not p:
        return jsonify({'ok': False, 'erro': 'PDF não encontrado'}), 404
    if p.get('arquivo_key'):
        try: dados = r2_download(p['arquivo_key'])
        except Exception: return jsonify({'ok': False, 'erro': 'Erro ao ler PDF do storage'})
    elif p.get('arquivo'):
        dados = bytes(p['arquivo'])
    else:
        return jsonify({'ok': False, 'erro': 'PDF não encontrado'}), 404
    valor = extrair_valor_pdf(dados)
    sql2 = 'UPDATE pdfs SET valor=%s WHERE id=%s AND user_id=%s' if USE_PG else 'UPDATE pdfs SET valor=? WHERE id=? AND user_id=?'
    db_exec(sql2, (valor, id, uid))
    return jsonify({'ok': True, 'valor': valor})

# ── Telegram webhook (público — chamado pelo servidor do Telegram) ─────────────

def _telegram_processar(uid, token, data):
    """Salva o chat_id no user_config do dono e responde confirmando."""
    message = data.get('message') or data.get('edited_message') or {}
    chat_id = str(message.get('chat', {}).get('id', ''))
    if not chat_id:
        return
    save_user_config(uid, {'telegram_chat_id': chat_id})
    cfg_u   = get_user_config(uid)
    nome    = message.get('from', {}).get('first_name', 'você')
    empresa = cfg_u.get('empresa_nome', 'ABRIU')
    resposta = (f"✅ Olá, {nome}! Tudo certo.\n\n"
                f"Você receberá notificações do *{empresa}* sempre que um cliente abrir um orçamento. 📋")
    try:
        requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                      json={'chat_id': chat_id, 'text': resposta, 'parse_mode': 'Markdown'}, timeout=10)
    except Exception:
        pass

@app.route('/telegram/webhook/<int:uid>', methods=['POST'])
@csrf_exempt
def telegram_webhook_user(uid):
    """Webhook por usuário: o dono é identificado pela URL e validado pelo secret_token
    que só o Telegram daquele bot conhece (impede sequestro de chat_id entre contas)."""
    cfg    = get_user_config(uid)
    secret = cfg.get('telegram_webhook_secret', '')
    sent   = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not secret or not secrets.compare_digest(sent, secret):
        return '', 403
    try:
        _telegram_processar(uid, cfg.get('telegram_token', ''), request.get_json(silent=True) or {})
    except Exception:
        pass
    return jsonify({'ok': True})

@app.route('/telegram/webhook', methods=['POST'])
@csrf_exempt
def telegram_webhook():
    """Rota legada (single-tenant) — mantida só para bots já registrados antes da
    migração por usuário. Novas conexões usam /telegram/webhook/<uid>."""
    try:
        sql_any = ('SELECT user_id, valor FROM user_config WHERE chave=%s LIMIT 1' if USE_PG
                   else 'SELECT user_id, valor FROM user_config WHERE chave=? LIMIT 1')
        row = db_exec(sql_any, ('telegram_token',), fetch='one')
        if row:
            _telegram_processar(row['user_id'], row['valor'], request.get_json(silent=True) or {})
    except Exception:
        pass
    return jsonify({'ok': True})

@app.route('/telegram/conectar', methods=['POST'])
@login_required
def telegram_conectar():
    """Registra o webhook do bot e retorna o link para o usuário abrir."""
    from flask import g
    uid = g.current_user['id']
    cfg = get_user_config(uid)
    token = cfg.get('telegram_token', '').strip()
    if not token:
        return jsonify({'ok': False, 'erro': 'Cole o token do bot primeiro.'})
    try:
        info = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=10).json()
        if not info.get('ok'):
            return jsonify({'ok': False, 'erro': 'Token inválido. Verifique e tente novamente.'})
        username = info['result']['username']
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)})
    base = get_base_url()
    # secret_token por usuário: o Telegram o reenvia em cada update, provando a origem
    secret = cfg.get('telegram_webhook_secret') or secrets.token_hex(16)
    save_user_config(uid, {'telegram_webhook_secret': secret})
    wh = requests.post(f'https://api.telegram.org/bot{token}/setWebhook',
                       json={'url': f'{base}/telegram/webhook/{uid}', 'secret_token': secret},
                       timeout=10).json()
    if not wh.get('ok'):
        return jsonify({'ok': False, 'erro': 'Falha ao registrar: ' + wh.get('description', '')})
    return jsonify({'ok': True, 'username': username,
                    'link': f'https://t.me/{username}',
                    'conectado': bool(cfg.get('telegram_chat_id'))})

@app.route('/telegram/status', methods=['GET'])
@login_required
def telegram_status():
    from flask import g
    uid = g.current_user['id']
    cfg = get_user_config(uid)
    return jsonify({'chat_id': cfg.get('telegram_chat_id', ''),
                    'conectado': bool(cfg.get('telegram_chat_id'))})

# ── configurações ─────────────────────────────────────────────────────────────

@app.route('/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    from flask import g
    uid = g.current_user['id']
    if request.method == 'POST':
        save_user_config(uid, request.get_json())
        return jsonify({'ok': True})
    u = g.current_user
    plano = u.get('plano') or 'free'
    stripe_ok = bool(os.environ.get('STRIPE_SECRET_KEY') and os.environ.get('STRIPE_PRICE_PRO'))
    return render_template('configuracoes.html', cfg=get_user_config(uid),
                           plano=plano, stripe_ok=stripe_ok)

@app.route('/testar_whatsapp', methods=['POST'])
@login_required
def testar_whatsapp():
    d = request.get_json()
    numero, apikey = d.get('numero','').strip(), d.get('apikey','').strip()
    if not numero or not apikey:
        return jsonify({'ok': False, 'erro': 'Preencha número e API Key'})
    ok, resp = notificar_whatsapp(numero, apikey, '✅ Teste do app de Orçamentos! Funcionando.')
    return jsonify({'ok': ok, 'resposta': resp})

@app.route('/testar_zapi', methods=['POST'])
@login_required
def testar_zapi():
    d = request.get_json()
    instance = d.get('instance','').strip()
    token = d.get('token','').strip()
    client_token = d.get('client_token','').strip()
    phone = d.get('phone','').strip()
    if not all([instance, token, client_token, phone]):
        return jsonify({'ok': False, 'erro': 'Preencha todos os campos'})
    ok, resp = notificar_zapi(instance, token, client_token, phone, '✅ Teste do app de Orçamentos! WhatsApp funcionando!')
    return jsonify({'ok': ok, 'resposta': resp})

@app.route('/testar_telegram', methods=['POST'])
@login_required
def testar_telegram():
    d = request.get_json()
    token, chat_id = d.get('token','').strip(), d.get('chat_id','').strip()
    if not token or not chat_id:
        return jsonify({'ok': False, 'erro': 'Preencha o token e o chat ID'})
    ok, resp = notificar_telegram(token, chat_id, '✅ Teste do app de Orçamentos! Notificações funcionando.')
    return jsonify({'ok': ok, 'resposta': resp})

@app.route('/testar_email', methods=['POST'])
@login_required
def testar_email():
    from flask import g
    d = request.get_json()
    html = '<div style="font-family:sans-serif;padding:2rem"><h2 style="color:#16a34a">✅ Email funcionando!</h2><p>Notificações do ABRIU configuradas com sucesso via Gmail.</p></div>'
    # Modo Gmail OAuth
    if d.get('gmail_oauth'):
        cfg = get_user_config(g.current_user['id'])
        if not cfg.get('gmail_refresh_token') or not cfg.get('gmail_email'):
            return jsonify({'ok': False, 'erro': 'Gmail não conectado'})
        ok, erro = notificar_email_gmail(cfg['gmail_refresh_token'], cfg['gmail_email'], html, subject='✅ Teste ABRIU — Gmail funcionando!')
        return jsonify({'ok': ok, 'erro': erro})
    # Modo SMTP legado
    email, senha = d.get('email','').strip(), d.get('senha','').strip()
    if not email or not senha:
        return jsonify({'ok': False, 'erro': 'Preencha email e senha'})
    ok, erro = notificar_email(email, senha, html)
    return jsonify({'ok': ok, 'erro': erro})

@app.route('/sse')
@login_required
def sse():
    """Server-Sent Events — streaming de notificações em tempo real para o usuário logado."""
    from flask import g
    uid = int(g.current_user['id'])
    q   = queue.Queue(maxsize=20)
    with _sse_lock:
        _sse_queues.setdefault(uid, []).append(q)

    # Verifica se o usuário precisa configurar notificações
    precisa_configurar = not user_config_completo(uid)

    def generate():
        try:
            # Evento de boas-vindas / onboarding
            if precisa_configurar:
                payload = json.dumps({
                    'tipo': 'onboarding',
                    'titulo': 'Configure suas notificações',
                    'msg': 'Receba alertas no WhatsApp quando o cliente abrir o orçamento.'
                })
                yield f'event: aviso\ndata: {payload}\n\n'
            else:
                yield ': conectado\n\n'

            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ': heartbeat\n\n'
        finally:
            with _sse_lock:
                lst = _sse_queues.get(uid, [])
                if q in lst:
                    lst.remove(q)
                if not lst:
                    _sse_queues.pop(uid, None)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})

@app.route('/push/vapid-key')
def push_vapid_key():
    """Retorna a chave pública VAPID para registro de push no browser."""
    _, pub = get_vapid_keys()
    return jsonify({'publicKey': pub})

@app.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    from flask import g
    uid  = g.current_user['id']
    sub  = request.get_json()
    if not sub or not sub.get('endpoint'):
        return jsonify({'ok': False}), 400
    endpoint = sub.get('endpoint', '')
    sub_json = json.dumps(sub)
    # Upsert: remove old subscription for same endpoint, insert new
    if USE_PG:
        db_exec('DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint=%s', (uid, endpoint))
        db_exec('INSERT INTO push_subscriptions(user_id,subscription_json,endpoint,criado_em) VALUES(%s,%s,%s,%s)',
                (uid, sub_json, endpoint, now_str()))
    else:
        db_exec('DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?', (uid, endpoint))
        db_exec('INSERT INTO push_subscriptions(user_id,subscription_json,endpoint,criado_em) VALUES(?,?,?,?)',
                (uid, sub_json, endpoint, now_str()))
    return jsonify({'ok': True})

@app.route('/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    from flask import g
    uid  = g.current_user['id']
    sub  = request.get_json()
    endpoint = (sub or {}).get('endpoint', '')
    if endpoint:
        if USE_PG:
            db_exec('DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint=%s', (uid, endpoint))
        else:
            db_exec('DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?', (uid, endpoint))
    return jsonify({'ok': True})

@app.route('/sw.js')
def service_worker():
    """Serve o SW da raiz para que tenha escopo sobre todo o site."""
    resp = send_file(os.path.join(app.root_path, 'static', 'sw.js'),
                     mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/ping')
def ping():
    return jsonify({'ok': True, 'status': 'alive'})

@app.route('/termos')
@app.route('/privacidade')
def termos():
    return render_template('termos.html')

@app.route('/healthz')
def healthz():
    return jsonify({'status': 'healthy', 'service': 'abriu'}), 200

@app.route('/robots.txt')
def robots():
    return send_file(os.path.join(app.root_path, 'static', 'robots.txt'), mimetype='text/plain')

@app.route('/sitemap.xml')
def sitemap():
    return send_file(os.path.join(app.root_path, 'static', 'sitemap.xml'), mimetype='application/xml')

# ── pagamento (Stripe) ────────────────────────────────────────────────────────

def get_stripe_price_id(plano):
    """Retorna o Price ID do Stripe para o plano. Cria se não existir."""
    import stripe
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
    env_key = 'STRIPE_PRICE_ANUAL' if plano == 'anual' else 'STRIPE_PRICE_MENSAL'
    price_id = os.environ.get(env_key, '')
    if price_id:
        return price_id
    # Cria produto e preço dinamicamente na primeira vez
    cfg_key = f'stripe_price_{plano}'
    cfg = get_config()
    if cfg.get(cfg_key):
        return cfg[cfg_key]
    try:
        product = stripe.Product.create(name='ABRIU Pro')
        if plano == 'anual':
            price = stripe.Price.create(
                product=product.id, unit_amount=18000, currency='brl',
                recurring={'interval': 'year'})
        else:
            price = stripe.Price.create(
                product=product.id, unit_amount=1990, currency='brl',
                recurring={'interval': 'month'})
        save_config({cfg_key: price.id})
        return price.id
    except Exception:
        return ''

@app.route('/paywall')
def paywall():
    if not session.get('user_email'):
        return redirect(url_for('login'))
    falha    = request.args.get('falha', '')
    pendente = request.args.get('pendente', '')
    return render_template('paywall.html', falha=falha, pendente=pendente)

@app.route('/assinar/<plano_id>')
@_rate_limit('10 per minute')
@login_required
def assinar_plano(plano_id):
    """GET redirect para checkout Stripe via card de plano na página de configurações."""
    if plano_id not in ('pro', 'agency'):
        return redirect('/configuracoes')
    price_key = 'STRIPE_PRICE_PRO' if plano_id == 'pro' else 'STRIPE_PRICE_AGENCY'
    price_id = os.environ.get(price_key, '')
    secret_key = os.environ.get('STRIPE_SECRET_KEY', '')
    if not price_id or not secret_key:
        return redirect('/configuracoes')
    import stripe as _stripe
    _stripe.api_key = secret_key
    u = g.current_user
    base = get_base_url()
    sess = _stripe.checkout.Session.create(
        payment_method_types=['card'],
        mode='subscription',
        line_items=[{'price': price_id, 'quantity': 1}],
        success_url=base + '/stripe/sucesso?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=base + '/configuracoes?stripe=cancelado',
        customer_email=u['email'],
        metadata={'email': u['email'], 'plano': plano_id},
    )
    return redirect(sess.url, 303)

@app.route('/assinar', methods=['POST'])
def assinar():
    if not session.get('user_email'):
        return redirect(url_for('login'))
    plano      = request.form.get('plano', 'mensal')
    secret_key = os.environ.get('STRIPE_SECRET_KEY', '')
    if not secret_key:
        return redirect(url_for('paywall') + '?falha=sem_chave')
    try:
        import stripe
        stripe.api_key = secret_key
        base     = get_base_url()
        price_id = get_stripe_price_id(plano)
        if not price_id:
            return redirect(url_for('paywall') + '?falha=1')
        checkout = stripe.checkout.Session.create(
            mode='subscription',
            customer_email=session['user_email'],
            line_items=[{'price': price_id, 'quantity': 1}],
            metadata={'email': session['user_email'], 'plano': plano},
            success_url=f"{base}/pagamento/sucesso?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/paywall?falha=cancelado",
            payment_method_types=['card'],
            locale='pt-BR',
            allow_promotion_codes=True,
        )
        return redirect(checkout.url)
    except Exception:
        return redirect(url_for('paywall') + '?falha=1')

@app.route('/pagamento/sucesso')
def pagamento_sucesso():
    from datetime import timedelta
    session_id = request.args.get('session_id', '')
    email      = session.get('user_email', '')
    if session_id and email:
        try:
            import stripe
            stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
            checkout = stripe.checkout.Session.retrieve(session_id)
            if checkout.payment_status in ('paid', 'no_payment_required'):
                plano = (checkout.metadata or {}).get('plano', 'mensal')
                dias  = 365 if plano == 'anual' else 30
                ends  = (datetime.now(BRASILIA) + timedelta(days=dias)).strftime('%Y-%m-%d %H:%M:%S')
                sql   = 'UPDATE users SET subscription_status=%s, subscription_ends_at=%s WHERE email=%s' if USE_PG else \
                        'UPDATE users SET subscription_status=?, subscription_ends_at=? WHERE email=?'
                db_exec(sql, ('ativo', ends, email.lower()))
                cfg = get_config()
                hora = datetime.now(BRASILIA).strftime('%d/%m/%Y às %H:%M')
                txt  = f"💳 Nova assinatura Stripe! {email} — plano {plano} às {hora}"
                threading.Thread(target=lambda: notificar(cfg, txt), daemon=True).start()
        except Exception:
            pass
    return redirect(url_for('dashboard') + '?pagamento=ok')

@app.route('/pagamento/falha')
def pagamento_falha():
    return redirect(url_for('paywall') + '?falha=1')

@app.route('/webhook/stripe', methods=['POST'])
@csrf_exempt
def webhook_stripe():
    """Webhook do Stripe — confirma pagamentos recorrentes e cancelamentos."""
    from datetime import timedelta
    import stripe
    stripe.api_key     = os.environ.get('STRIPE_SECRET_KEY', '')
    webhook_secret     = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    payload            = request.get_data()
    sig                = request.headers.get('Stripe-Signature', '')
    # Segurança: sem o secret não há como verificar a assinatura → eventos forjados
    # poderiam ativar assinaturas de graça. Exigimos o secret e rejeitamos se faltar.
    if not webhook_secret:
        log.error('webhook_stripe: STRIPE_WEBHOOK_SECRET nao configurado — evento rejeitado. '
                  'Configure em Render > Environment para ativar o webhook.')
        return jsonify({'ok': False, 'erro': 'webhook nao configurado'}), 503
    try:
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
    except Exception:
        return jsonify({'ok': False}), 400

    etype = event['type']
    obj   = event['data']['object']

    if etype == 'checkout.session.completed':
        email = (obj.get('metadata') or {}).get('email') or obj.get('customer_email', '')
        plano = (obj.get('metadata') or {}).get('plano', 'mensal')
        if email and obj.get('payment_status') in ('paid', 'no_payment_required'):
            dias = 365 if plano == 'anual' else 30
            ends = (datetime.now(BRASILIA) + timedelta(days=dias)).strftime('%Y-%m-%d %H:%M:%S')
            sql  = 'UPDATE users SET subscription_status=%s, subscription_ends_at=%s WHERE email=%s' if USE_PG else \
                   'UPDATE users SET subscription_status=?, subscription_ends_at=? WHERE email=?'
            db_exec(sql, ('ativo', ends, email.lower()))

    elif etype == 'invoice.payment_succeeded':
        # Renovação mensal/anual automática
        customer_email = obj.get('customer_email', '')
        sub_id = obj.get('subscription', '')
        if customer_email and sub_id:
            try:
                sub  = stripe.Subscription.retrieve(sub_id)
                plano = 'anual' if sub['items']['data'][0]['price']['recurring']['interval'] == 'year' else 'mensal'
                dias = 365 if plano == 'anual' else 30
                ends = (datetime.now(BRASILIA) + timedelta(days=dias)).strftime('%Y-%m-%d %H:%M:%S')
                sql  = 'UPDATE users SET subscription_status=%s, subscription_ends_at=%s WHERE email=%s' if USE_PG else \
                       'UPDATE users SET subscription_status=?, subscription_ends_at=? WHERE email=?'
                db_exec(sql, ('ativo', ends, customer_email.lower()))
            except Exception:
                pass

    elif etype in ('customer.subscription.deleted', 'customer.subscription.paused'):
        customer_email = obj.get('customer_email', '')
        if not customer_email:
            try:
                customer_email = stripe.Customer.retrieve(obj['customer']).email or ''
            except Exception:
                pass
        if customer_email:
            sql = 'UPDATE users SET subscription_status=%s WHERE email=%s' if USE_PG else \
                  'UPDATE users SET subscription_status=? WHERE email=?'
            db_exec(sql, ('cancelado', customer_email.lower()))

    return jsonify({'ok': True})

if __name__ == '__main__':
    init_db()
    log.info('App iniciado em http://localhost:5000')
    app.run(debug=False, port=5000)

# Para produção (gunicorn)
try:
    init_db()
except Exception as _e:
    log.error('init_db falhou no startup: %s', _e)
