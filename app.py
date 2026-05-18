# gevent monkey-patch deve ser a primeira coisa — antes de qualquer import
try:
    from gevent import monkey as _gm; _gm.patch_all()
except ImportError:
    pass

import os, json, uuid, requests, smtplib, threading, re, io, base64, queue
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

BRASILIA = ZoneInfo('America/Sao_Paulo')
from flask import Flask, render_template, request, redirect, jsonify, send_file, Response, session, url_for, flash
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import io

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.secret_key = os.environ.get('SECRET_KEY', 'orceveja-secret-2025-local')

# Gzip em todas as respostas (HTML, JSON, CSS, JS) — reduz ~70% do tamanho
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

DATABASE_URL = os.environ.get('DATABASE_URL')
USE_PG = bool(DATABASE_URL)

# Redireciona www → apex permanentemente
@app.before_request
def redirect_www():
    host = request.host.split(':')[0]
    if host.startswith('www.'):
        apex = host[4:]
        url = request.url.replace(f'https://{host}', f'https://{apex}', 1) \
                         .replace(f'http://{host}',  f'https://{apex}', 1)
        return redirect(url, code=301)

# Cache de estáticos (1 ano para CSS/JS imutáveis, sem cache para SW)
@app.after_request
def cache_headers(response):
    path = request.path
    if path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif path == '/sw.js':
        response.headers['Cache-Control'] = 'no-cache'
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
        print(f'VAPID key error: {e}')
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
            print(f'Web push error: {ex}')
    threading.Thread(target=_worker, daemon=True).start()

# ── banco de dados ──────────────────────────────────────────────────────────

# ── Connection pool (PostgreSQL) ─────────────────────────────────────────────
_pg_pool = None
_pg_pool_lock = threading.Lock()

def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        with _pg_pool_lock:
            if _pg_pool is None:
                import psycopg2.pool
                try:
                    from psycogreen.gevent import patch_psycopg
                    patch_psycopg()
                except Exception:
                    pass
                url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
                _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=10, dsn=url,
                    connect_timeout=10,
                    options='-c statement_timeout=10000'
                )
    return _pg_pool

def _pg_connect():
    """Conexão direta sem pool — usada apenas no init_db."""
    import psycopg2
    url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return psycopg2.connect(url, connect_timeout=30)

def get_db():
    if USE_PG:
        return _get_pg_pool().getconn()
    else:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), 'orcamentos.db')
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return con

def db_exec(sql, params=(), fetch=None):
    con = get_db()
    try:
        if USE_PG:
            import psycopg2.extras
            cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            sql_pg = sql.replace('?', '%s').replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
            cur.execute(sql_pg, params)
            con.commit()
            if fetch == 'all': return [dict(r) for r in cur.fetchall()]
            if fetch == 'one': r = cur.fetchone(); return dict(r) if r else None
        else:
            cur = con.execute(sql, params)
            con.commit()
            if fetch == 'all': return [dict(r) for r in cur.fetchall()]
            if fetch == 'one': r = cur.fetchone(); return dict(r) if r else None
    finally:
        if USE_PG:
            _get_pg_pool().putconn(con)   # devolve ao pool em vez de fechar
        else:
            con.close()

def init_db():
    if USE_PG:
        con = _pg_connect()
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
        con.commit()
        con.close()
    else:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), 'orcamentos.db')
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
            CREATE INDEX IF NOT EXISTS idx_pdfs_user   ON pdfs(user_id);
            CREATE INDEX IF NOT EXISTS idx_acessos_pdf ON pdf_acessos(pdf_id);
            CREATE INDEX IF NOT EXISTS idx_push_user   ON push_subscriptions(user_id);
        ''')
        # Migração: adicionar user_id à tabela pdfs (SQLite não suporta IF NOT EXISTS)
        try:
            con.execute('ALTER TABLE pdfs ADD COLUMN user_id INTEGER')
            con.commit()
        except Exception:
            pass
        con.commit()
        con.close()

def get_config():
    rows = db_exec('SELECT chave, valor FROM config', fetch='all')
    return {r['chave']: r['valor'] for r in rows} if rows else {}

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

@app.route('/login', methods=['GET', 'POST'])
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
        elif len(senha) < 6:
            erro = 'A senha deve ter pelo menos 6 caracteres.'
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
    txt = (f"🆕 Novo cadastro no OrcEVeja!\n\n"
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
    params = ('client_id=' + client_id +
              '&redirect_uri=' + requests.utils.quote(base + '/auth/gmail/callback') +
              '&response_type=code'
              '&scope=' + requests.utils.quote('https://www.googleapis.com/auth/gmail.send email profile') +
              '&access_type=offline'
              '&prompt=consent')
    return redirect('https://accounts.google.com/o/oauth2/v2/auth?' + params)

@app.route('/auth/gmail/callback')
@login_required
def auth_gmail_callback():
    code = request.args.get('code', '')
    if not code:
        return redirect(url_for('configuracoes') + '?gmail_erro=cancelado')
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
        save_config({'gmail_refresh_token': refresh_token, 'gmail_email': gmail_email})
    except Exception as e:
        return redirect(url_for('configuracoes') + '?gmail_erro=excecao')
    return redirect(url_for('configuracoes') + '?gmail_ok=1')

@app.route('/auth/gmail/disconnect', methods=['POST'])
@login_required
def auth_gmail_disconnect():
    save_config({'gmail_refresh_token': '', 'gmail_email': ''})
    return jsonify({'ok': True})

@app.route('/gmail/status')
@login_required
def gmail_status():
    cfg = get_config()
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
                client = anthropic.Anthropic(api_key=anthropic_key)
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

def notificar_email_gmail(refresh_token, sender_email, html, subject='👁 Notificação OrcEVeja'):
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
    def _enviar():
        try:
            if cfg.get('whatsapp_numero') and cfg.get('whatsapp_apikey'):
                notificar_whatsapp(cfg['whatsapp_numero'], cfg['whatsapp_apikey'], txt)
            # Gmail OAuth tem prioridade; cai no SMTP só se não tiver refresh_token
            if cfg.get('gmail_refresh_token') and cfg.get('gmail_email'):
                notificar_email_gmail(cfg['gmail_refresh_token'], cfg['gmail_email'], html or f'<p>{txt}</p>')
            elif cfg.get('email_remetente') and cfg.get('email_senha_app'):
                notificar_email(cfg['email_remetente'], cfg['email_senha_app'], html or f'<p>{txt}</p>')
            if cfg.get('telegram_token') and cfg.get('telegram_chat_id'):
                notificar_telegram(cfg['telegram_token'], cfg['telegram_chat_id'], txt)
            if cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token') and cfg.get('zapi_phone'):
                notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'], cfg['zapi_phone'], txt)
        except BaseException:
            pass
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
    sql = 'SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas,valor FROM pdfs WHERE user_id=%s ORDER BY criado_em DESC' if USE_PG else \
          'SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas,valor FROM pdfs WHERE user_id=? ORDER BY criado_em DESC'
    pdfs = db_exec(sql, (uid,), fetch='all') or []
    for p in pdfs:
        p['valor'] = float(p['valor'] or 0)
    dias = dias_trial_restantes(u)
    return render_template('dashboard.html', pdfs=pdfs, dias_trial=dias,
                           subscription_status=u.get('subscription_status') if u else None)

@app.route('/criar', methods=['GET', 'POST'])
def criar():
    if request.method == 'POST':
        d = request.get_json()
        token = str(uuid.uuid4())[:12].strip('-')
        if USE_PG:
            db_exec('INSERT INTO orcamentos(token,cliente_nome,cliente_telefone,titulo,itens,observacoes,prazo,forma_pagamento,validade,status,criado_em) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (token, d['cliente_nome'], d.get('cliente_telefone',''), d['titulo'],
                 json.dumps(d['itens'],ensure_ascii=False), d.get('observacoes',''),
                 d.get('prazo',''), d.get('forma_pagamento',''), d.get('validade',''), 'rascunho', now_str()))
        else:
            db_exec('INSERT INTO orcamentos(token,cliente_nome,cliente_telefone,titulo,itens,observacoes,prazo,forma_pagamento,validade,status,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (token, d['cliente_nome'], d.get('cliente_telefone',''), d['titulo'],
                 json.dumps(d['itens'],ensure_ascii=False), d.get('observacoes',''),
                 d.get('prazo',''), d.get('forma_pagamento',''), d.get('validade',''), 'rascunho', now_str()))
        return jsonify({'ok': True, 'token': token})
    return render_template('criar.html', orcamento=None)

@app.route('/editar/<int:id>', methods=['GET', 'POST'])
def editar(id):
    if request.method == 'POST':
        d = request.get_json()
        p = (d['cliente_nome'], d.get('cliente_telefone',''), d['titulo'],
             json.dumps(d['itens'],ensure_ascii=False), d.get('observacoes',''),
             d.get('prazo',''), d.get('forma_pagamento',''), d.get('validade',''), id)
        sql = 'UPDATE orcamentos SET cliente_nome=%s,cliente_telefone=%s,titulo=%s,itens=%s,observacoes=%s,prazo=%s,forma_pagamento=%s,validade=%s WHERE id=%s' if USE_PG else \
              'UPDATE orcamentos SET cliente_nome=?,cliente_telefone=?,titulo=?,itens=?,observacoes=?,prazo=?,forma_pagamento=?,validade=? WHERE id=?'
        db_exec(sql, p)
        return jsonify({'ok': True})
    sql = 'SELECT * FROM orcamentos WHERE id=%s' if USE_PG else 'SELECT * FROM orcamentos WHERE id=?'
    o = db_exec(sql, (id,), fetch='one')
    if not o: return redirect(url_for('dashboard'))
    o['itens'] = json.loads(o['itens'])
    return render_template('criar.html', orcamento=o)

@app.route('/deletar/<int:id>', methods=['POST'])
def deletar(id):
    sql = 'DELETE FROM orcamentos WHERE id=%s' if USE_PG else 'DELETE FROM orcamentos WHERE id=?'
    db_exec(sql, (id,))
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
def marcar_enviado(id):
    sql = "UPDATE orcamentos SET status='enviado' WHERE id=%s" if USE_PG else \
          "UPDATE orcamentos SET status='enviado' WHERE id=?"
    db_exec(sql, (id,))
    return jsonify({'ok': True})

@app.route('/atualizar_status/<int:id>', methods=['POST'])
def atualizar_status(id):
    d = request.get_json()
    status = d.get('status','')
    if status not in ['fechou','negociando','perdido','enviado','aberto']:
        return jsonify({'ok': False})
    sql = 'UPDATE orcamentos SET status=%s WHERE id=%s' if USE_PG else 'UPDATE orcamentos SET status=? WHERE id=?'
    db_exec(sql, (status, id))
    return jsonify({'ok': True})

@app.route('/link/<token>')
def gerar_link(token):
    return jsonify({'link': f"{get_base_url()}/ver/{token}"})

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
@login_required
def upload_pdf():
    if 'pdf' not in request.files:
        return jsonify({'ok': False, 'erro': 'Nenhum arquivo enviado'})
    f = request.files['pdf']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'erro': 'Apenas PDFs são aceitos'})
    dados_originais = f.read()
    token = str(uuid.uuid4()).replace('-','')[:16]
    filename = secure_filename(f.filename)
    valor_manual = request.form.get('valor', '').replace('R$','').replace('.','').replace(',','.').strip()
    try: valor = float(valor_manual) if valor_manual else None
    except: valor = None
    # Se não informou valor manualmente, tenta extrair do PDF com IA
    if not valor:
        valor = extrair_valor_pdf(dados_originais) or 0.0
    # Comprime o PDF e emite rastreador embutido no arquivo
    dados = comprimir_pdf(dados_originais)
    tracking_url = f"{get_base_url()}/track/{token}"
    dados = embed_tracker_pdf(dados, tracking_url)
    from flask import g
    uid = g.current_user['id']
    if USE_PG:
        import psycopg2
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,titulo,arquivo,filename,status,criado_em,valor,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             request.form.get('titulo', filename), psycopg2.Binary(dados), filename, 'enviado', now_str(), valor, uid))
    else:
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,titulo,arquivo,filename,status,criado_em,valor,user_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             request.form.get('titulo', filename), dados, filename, 'enviado', now_str(), valor, uid))
    link = f"{get_base_url()}/pdf/{token}"
    cliente_nome = request.form.get('cliente_nome', 'Cliente')
    cliente_tel = request.form.get('cliente_telefone', '').strip()
    titulo = request.form.get('titulo', filename)

    # Enviar arquivo PDF diretamente via WhatsApp (com rastreador embutido)
    cfg = get_user_config(uid)
    if cliente_tel and cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token'):
        phone = '55' + cliente_tel if not cliente_tel.startswith('55') else cliente_tel
        caption = f"Olá, {cliente_nome}! 👋\n\nSegue o orçamento *{titulo}* conforme solicitado.\n\nQualquer dúvida estou à disposição!"
        fname   = filename or 'orcamento.pdf'
        pdf_snapshot = bytes(dados)
        link_msg = f"🔗 Para visualizar online: {link}"
        def _enviar_cliente(pdf=pdf_snapshot, fn=fname, cap=caption, lm=link_msg):
            try:
                notificar_zapi_documento(
                    cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                    phone, pdf, fn, cap)
                import time; time.sleep(1)  # pequeno delay para manter ordem
                notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                               phone, lm)
            except Exception:
                pass
        threading.Thread(target=_enviar_cliente, daemon=True).start()

    return jsonify({'ok': True, 'token': token, 'link': link, 'id': db_exec('SELECT id FROM pdfs WHERE token=%s' if USE_PG else 'SELECT id FROM pdfs WHERE token=?', (token,), fetch='one')['id'], 'valor': valor, 'titulo': request.form.get('titulo', filename), 'tel': cliente_tel})

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
    sql = 'SELECT * FROM pdfs WHERE token=%s' if USE_PG else 'SELECT * FROM pdfs WHERE token=?'
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
    """Registra abertura e serve o PDF. Bloqueia e notifica se o link expirou (48h)."""
    from datetime import timedelta
    sql = 'SELECT * FROM pdfs WHERE token=%s' if USE_PG else 'SELECT * FROM pdfs WHERE token=?'
    p = db_exec(sql, (token,), fetch='one')
    if not p: return 'Orçamento não encontrado.', 404

    # ── Verifica expiração (48h após criado_em) ──────────────────────────────
    cfg = get_config()
    try:
        criado = datetime.strptime(p['criado_em'][:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BRASILIA)
    except Exception:
        criado = datetime.now(BRASILIA)
    expira = criado + timedelta(hours=PRAZO_HORAS)
    agora  = datetime.now(BRASILIA)
    expirado = agora > expira

    if expirado:
        # Registra tentativa e notifica
        hora = agora.strftime('%H:%M de %d/%m')
        txt = (f"⏰ LINK EXPIRADO — {p['cliente_nome']} tentou abrir "
               f"'{p['titulo']}' às {hora}, mas o link de 48h já venceu.")
        html_notif = f"""<div style="font-family:sans-serif;padding:2rem;background:#fef2f2;border-radius:12px">
            <h2 style="color:#dc2626">⏰ Link Expirado!</h2>
            <p><strong>{p['cliente_nome']}</strong> tentou abrir <strong>{p['titulo']}</strong>
            às <strong>{hora}</strong>, mas o link de 48h já venceu.<br><br>
            Acesse o dashboard para gerar um novo link.</p></div>"""
        owner = p.get('user_id')
        cfg   = get_user_config(owner) if owner else {}
        threading.Thread(target=lambda: notificar(cfg, txt, html_notif), daemon=True).start()
        empresa = cfg.get('empresa_nome', 'a empresa')
        return render_template('expirado.html', cliente=p['cliente_nome'],
                               titulo=p['titulo'], empresa=empresa), 410

    # ── Registra abertura normal ─────────────────────────────────────────────
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    ua = request.headers.get('User-Agent', '')
    registrar_acesso(p['id'], token, ip, ua, 'link')
    aberturas = (p['aberturas'] or 0) + 1
    primeira  = p['status'] == 'enviado'
    if USE_PG:
        db_exec("UPDATE pdfs SET aberturas=%s,aberto_em=%s,status=%s WHERE token=%s",
            (aberturas, now_str(), 'aberto', token))
    else:
        db_exec("UPDATE pdfs SET aberturas=?,aberto_em=?,status=? WHERE token=?",
            (aberturas, now_str(), 'aberto', token))
    hora  = agora.strftime('%H:%M')
    owner = p.get('user_id')
    cfg   = get_user_config(owner) if owner else {}
    if primeira:
        txt = f"👁 {p['cliente_nome']} abriu '{p['titulo']}' pela 1ª vez às {hora}!"
        html_notif = f"""<div style="font-family:sans-serif;padding:2rem;background:#f0fdf4;border-radius:12px">
            <h2 style="color:#16a34a">👁 PDF Visualizado!</h2>
            <p><strong>{p['cliente_nome']}</strong> abriu <strong>{p['titulo']}</strong> às <strong>{hora}</strong>.</p></div>"""
    else:
        txt = f"🔄 {p['cliente_nome']} abriu novamente '{p['titulo']}' às {hora}. Total: {aberturas}x"
        html_notif = f"""<div style="font-family:sans-serif;padding:2rem;background:#fffbeb;border-radius:12px">
            <h2 style="color:#f59e0b">🔄 PDF Aberto Novamente</h2>
            <p><strong>{p['cliente_nome']}</strong> abriu <strong>{p['titulo']}</strong> às <strong>{hora}</strong>. Total: <strong>{aberturas}x</strong></p></div>"""
    if owner:
        sse_push(owner, 'abriu', {'nome': p['cliente_nome'], 'titulo': p['titulo'], 'hora': hora})
        send_web_push(owner, f"👁 {p['cliente_nome']} abriu o orçamento!", p['titulo'])
    threading.Thread(target=lambda: notificar(cfg, txt, html_notif), daemon=True).start()
    arquivo  = bytes(p['arquivo']) if p['arquivo'] else b''
    filename = (p['filename'] or 'orcamento.pdf').replace('"', '')
    return Response(arquivo, mimetype='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{filename}"',
                 'Cache-Control': 'no-store'})

@app.route('/renovar_link/<int:id>', methods=['POST'])
@login_required
def renovar_link(id):
    """Gera novo token, reseta prazo de 48h e envia o novo link para o cliente."""
    from flask import g
    uid = g.current_user['id']
    # Busca dados do PDF antes de renovar (filtra por user_id para segurança)
    sql_sel = 'SELECT cliente_nome, cliente_telefone, titulo FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else \
              'SELECT cliente_nome, cliente_telefone, titulo FROM pdfs WHERE id=? AND user_id=?'
    p = db_exec(sql_sel, (id, uid), fetch='one')
    if not p:
        return jsonify({'ok': False, 'erro': 'PDF não encontrado'})

    novo_token = str(uuid.uuid4()).replace('-','')[:16]

    # Busca o arquivo para re-embutir o tracker com o novo token
    sql_arq = 'SELECT arquivo, filename FROM pdfs WHERE id=%s' if USE_PG else \
              'SELECT arquivo, filename FROM pdfs WHERE id=?'
    arq_row = db_exec(sql_arq, (id,), fetch='one')
    arquivo_atual = bytes(arq_row['arquivo']) if arq_row and arq_row['arquivo'] else b''
    fname = (arq_row.get('filename') or 'orcamento.pdf') if arq_row else 'orcamento.pdf'

    novo_tracking_url = f"{get_base_url()}/track/{novo_token}"
    arquivo_novo = embed_tracker_pdf(arquivo_atual, novo_tracking_url)

    if USE_PG:
        import psycopg2
        db_exec('UPDATE pdfs SET token=%s, criado_em=%s, status=%s, aberturas=0, aberto_em=NULL, arquivo=%s WHERE id=%s',
                (novo_token, now_str(), 'enviado', psycopg2.Binary(arquivo_novo), id))
    else:
        db_exec('UPDATE pdfs SET token=?, criado_em=?, status=?, aberturas=0, aberto_em=NULL, arquivo=? WHERE id=?',
                (novo_token, now_str(), 'enviado', arquivo_novo, id))

    link = f"{get_base_url()}/pdf/{novo_token}"

    # Envia arquivo PDF atualizado diretamente via WhatsApp
    cfg = get_user_config(uid)
    cliente_tel  = (p.get('cliente_telefone') or '').strip()
    cliente_nome = p.get('cliente_nome', 'Cliente')
    if cliente_tel and cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token'):
        phone   = '55' + cliente_tel if not cliente_tel.startswith('55') else cliente_tel
        caption = f"Olá, {cliente_nome}! 👋\n\nSegue o orçamento atualizado conforme solicitado.\n\nQualquer dúvida estou à disposição!"
        pdf_snap = bytes(arquivo_novo)
        link_msg = f"🔗 Para visualizar online: {link}"
        def _reenviar(pdf=pdf_snap, fn=fname, cap=caption, lm=link_msg):
            try:
                notificar_zapi_documento(
                    cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                    phone, pdf, fn, cap)
                import time; time.sleep(1)
                notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'],
                               phone, lm)
            except Exception:
                pass
        threading.Thread(target=_reenviar, daemon=True).start()

    return jsonify({'ok': True, 'token': novo_token, 'link': link,
                    'enviado_wpp': bool(cliente_tel)})

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
    sql = 'DELETE FROM pdfs WHERE id=%s AND user_id=%s' if USE_PG else 'DELETE FROM pdfs WHERE id=? AND user_id=?'
    db_exec(sql, (id, uid))
    return redirect(url_for('dashboard'))

@app.route('/deletar_pdf_ajax/<int:id>', methods=['POST'])
@login_required
def deletar_pdf_ajax(id):
    sql = 'DELETE FROM pdfs WHERE id=%s' if USE_PG else 'DELETE FROM pdfs WHERE id=?'
    db_exec(sql, (id,))
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
    sql = 'SELECT arquivo FROM pdfs WHERE id=%s' if USE_PG else 'SELECT arquivo FROM pdfs WHERE id=?'
    p = db_exec(sql, (id,), fetch='one')
    if not p or not p['arquivo']:
        return jsonify({'ok': False, 'erro': 'PDF não encontrado'})
    dados = bytes(p['arquivo'])
    valor = extrair_valor_pdf(dados)
    sql2 = 'UPDATE pdfs SET valor=%s WHERE id=%s' if USE_PG else 'UPDATE pdfs SET valor=? WHERE id=?'
    db_exec(sql2, (valor, id))
    return jsonify({'ok': True, 'valor': valor})

# ── Telegram webhook (público — chamado pelo servidor do Telegram) ─────────────

@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Recebe mensagens do bot e auto-registra o chat_id do usuário."""
    try:
        data = request.get_json(silent=True) or {}
        message = data.get('message') or data.get('edited_message') or {}
        chat = message.get('chat', {})
        chat_id = str(chat.get('id', ''))
        if chat_id:
            save_config({'telegram_chat_id': chat_id})
            cfg = get_config()
            token = cfg.get('telegram_token', '')
            if token:
                nome = message.get('from', {}).get('first_name', 'você')
                empresa = cfg.get('empresa_nome', 'OrcEVeja')
                resposta = (f"✅ Olá, {nome}! Tudo certo.\n\n"
                            f"Você receberá notificações do *{empresa}* sempre que um cliente abrir um orçamento. 📋")
                requests.post(
                    f'https://api.telegram.org/bot{token}/sendMessage',
                    json={'chat_id': chat_id, 'text': resposta, 'parse_mode': 'Markdown'},
                    timeout=10
                )
    except Exception:
        pass
    return jsonify({'ok': True})

@app.route('/telegram/conectar', methods=['POST'])
@login_required
def telegram_conectar():
    """Registra o webhook do bot e retorna o link para o usuário abrir."""
    cfg = get_config()
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
    wh = requests.post(f'https://api.telegram.org/bot{token}/setWebhook',
                       json={'url': f'{base}/telegram/webhook'}, timeout=10).json()
    if not wh.get('ok'):
        return jsonify({'ok': False, 'erro': 'Falha ao registrar: ' + wh.get('description', '')})
    return jsonify({'ok': True, 'username': username,
                    'link': f'https://t.me/{username}',
                    'conectado': bool(cfg.get('telegram_chat_id'))})

@app.route('/telegram/status', methods=['GET'])
@login_required
def telegram_status():
    cfg = get_config()
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
    return render_template('configuracoes.html', cfg=get_user_config(uid))

@app.route('/testar_whatsapp', methods=['POST'])
def testar_whatsapp():
    d = request.get_json()
    numero, apikey = d.get('numero','').strip(), d.get('apikey','').strip()
    if not numero or not apikey:
        return jsonify({'ok': False, 'erro': 'Preencha número e API Key'})
    ok, resp = notificar_whatsapp(numero, apikey, '✅ Teste do app de Orçamentos! Funcionando.')
    return jsonify({'ok': ok, 'resposta': resp})

@app.route('/testar_zapi', methods=['POST'])
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
def testar_telegram():
    d = request.get_json()
    token, chat_id = d.get('token','').strip(), d.get('chat_id','').strip()
    if not token or not chat_id:
        return jsonify({'ok': False, 'erro': 'Preencha o token e o chat ID'})
    ok, resp = notificar_telegram(token, chat_id, '✅ Teste do app de Orçamentos! Notificações funcionando.')
    return jsonify({'ok': ok, 'resposta': resp})

@app.route('/testar_email', methods=['POST'])
def testar_email():
    d = request.get_json()
    html = '<div style="font-family:sans-serif;padding:2rem"><h2 style="color:#16a34a">✅ Email funcionando!</h2><p>Notificações do OrcEVeja configuradas com sucesso via Gmail.</p></div>'
    # Modo Gmail OAuth
    if d.get('gmail_oauth'):
        cfg = get_config()
        if not cfg.get('gmail_refresh_token') or not cfg.get('gmail_email'):
            return jsonify({'ok': False, 'erro': 'Gmail não conectado'})
        ok, erro = notificar_email_gmail(cfg['gmail_refresh_token'], cfg['gmail_email'], html, subject='✅ Teste OrcEVeja — Gmail funcionando!')
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
    """Health check — usado por serviços externos para manter o servidor acordado."""
    return jsonify({'ok': True, 'status': 'alive'})

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
        product = stripe.Product.create(name='Orce & Veja Pro')
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
def webhook_stripe():
    """Webhook do Stripe — confirma pagamentos recorrentes e cancelamentos."""
    from datetime import timedelta
    import stripe
    stripe.api_key     = os.environ.get('STRIPE_SECRET_KEY', '')
    webhook_secret     = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    payload            = request.get_data()
    sig                = request.headers.get('Stripe-Signature', '')
    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        else:
            event = stripe.Event.construct_from(request.get_json(), stripe.api_key)
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
    print('\n✅ App de Orçamentos iniciado!')
    print('👉 Acesse: http://localhost:5000\n')
    app.run(debug=False, port=5000)

# Para produção (gunicorn)
try:
    init_db()
except Exception as _e:
    print(f'[WARN] init_db falhou no startup: {_e}. O app continuará e tentará novamente na primeira requisição.')
