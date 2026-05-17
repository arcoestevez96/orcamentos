import os, json, uuid, requests, smtplib, threading, re, io
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

DATABASE_URL = os.environ.get('DATABASE_URL')
USE_PG = bool(DATABASE_URL)

# ── banco de dados ──────────────────────────────────────────────────────────

def get_db():
    if USE_PG:
        import psycopg2, psycopg2.extras
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        con = psycopg2.connect(url)
        return con
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
        con.close()

def init_db():
    if USE_PG:
        con = get_db()
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
                password_hash TEXT NOT NULL,
                nome TEXT,
                criado_em TEXT NOT NULL
            )''')
        cur.execute('ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS valor NUMERIC DEFAULT 0')
        for k, v in [('whatsapp_numero',''),('whatsapp_apikey',''),
                     ('empresa_nome',''),('base_url',''),
                     ('email_remetente',''),('email_senha_app',''),
                     ('telegram_token',''),('telegram_chat_id',''),
                     ('zapi_instance',''),('zapi_token',''),('zapi_client_token',''),('zapi_phone','')]:
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
                password_hash TEXT NOT NULL,
                nome TEXT,
                criado_em TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
                cliente_nome TEXT NOT NULL, cliente_telefone TEXT, titulo TEXT NOT NULL,
                arquivo BLOB, filename TEXT, status TEXT DEFAULT 'enviado',
                criado_em TEXT NOT NULL, aberto_em TEXT, aberturas INTEGER DEFAULT 0
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
        ''')
        con.commit()
        con.close()

def get_config():
    rows = db_exec('SELECT chave, valor FROM config', fetch='all')
    return {r['chave']: r['valor'] for r in rows} if rows else {}

# ── autenticação ─────────────────────────────────────────────────────────────

def usuario_existe():
    r = db_exec('SELECT id FROM users LIMIT 1', fetch='one')
    return r is not None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_email'):
            return redirect(url_for('login'))
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
    # Cadastro só permitido se ainda não há usuário
    if usuario_existe() and not session.get('user_email'):
        return redirect(url_for('login'))
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
                db_exec(sql_ins, (email, ph, nome, now_str()))
                # Salva email nas configurações de notificação automaticamente
                save_config({'email_remetente': email})
                session['user_email'] = email
                session['user_nome'] = nome or email.split('@')[0]
                return redirect(url_for('dashboard'))
    return render_template('login.html', modo='cadastro', erro=erro)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

def save_config(dados):
    for k, v in dados.items():
        if USE_PG:
            db_exec('UPDATE config SET valor=%s WHERE chave=%s', (v, k))
        else:
            db_exec('UPDATE config SET valor=? WHERE chave=?', (v, k))

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

def notificar(cfg, txt, html=None):
    def _enviar():
        try:
            if cfg.get('whatsapp_numero') and cfg.get('whatsapp_apikey'):
                notificar_whatsapp(cfg['whatsapp_numero'], cfg['whatsapp_apikey'], txt)
            if cfg.get('email_remetente') and cfg.get('email_senha_app'):
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

# ── rotas ────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    pdfs = db_exec('SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas,valor FROM pdfs ORDER BY criado_em DESC', fetch='all') or []
    for p in pdfs:
        p['valor'] = float(p['valor'] or 0)
    return render_template('dashboard.html', pdfs=pdfs)

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
    if not o: return redirect('/')
    o['itens'] = json.loads(o['itens'])
    return render_template('criar.html', orcamento=o)

@app.route('/deletar/<int:id>', methods=['POST'])
def deletar(id):
    sql = 'DELETE FROM orcamentos WHERE id=%s' if USE_PG else 'DELETE FROM orcamentos WHERE id=?'
    db_exec(sql, (id,))
    return redirect('/')

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
        notificar(cfg, txt, html)
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
    rows = db_exec('SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas FROM pdfs ORDER BY criado_em DESC', fetch='all') or []
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
    # Comprime o PDF para abertura mais rápida
    dados = comprimir_pdf(dados_originais)
    if USE_PG:
        import psycopg2
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,titulo,arquivo,filename,status,criado_em,valor) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             request.form.get('titulo', filename), psycopg2.Binary(dados), filename, 'enviado', now_str(), valor))
    else:
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,titulo,arquivo,filename,status,criado_em,valor) VALUES(?,?,?,?,?,?,?,?,?)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             request.form.get('titulo', filename), dados, filename, 'enviado', now_str(), valor))
    link = f"{get_base_url()}/pdf/{token}"
    cliente_nome = request.form.get('cliente_nome', 'Cliente')
    cliente_tel = request.form.get('cliente_telefone', '').strip()
    titulo = request.form.get('titulo', filename)

    # Enviar link automaticamente para o WhatsApp do cliente via Z-API
    cfg = get_config()
    if cliente_tel and cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token'):
        phone = '55' + cliente_tel if not cliente_tel.startswith('55') else cliente_tel
        msg_cliente = f"OLÁ, {cliente_nome.upper()} !\n\nSEGUE O ORÇAMENTO/DOCUMENTO SOLICITADO.\n\n{link}"
        def _enviar_cliente():
            try:
                notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'], phone, msg_cliente)
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
        # Só usa a versão comprimida se for menor
        return compressed if len(compressed) < len(dados_bytes) else dados_bytes
    except Exception:
        return dados_bytes

PRAZO_HORAS = 48  # link expira após 48h

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
        threading.Thread(target=lambda: notificar(cfg, txt, html_notif), daemon=True).start()
        empresa = cfg.get('empresa_nome', 'a empresa')
        return render_template('expirado.html', cliente=p['cliente_nome'],
                               titulo=p['titulo'], empresa=empresa), 410

    # ── Registra abertura normal ─────────────────────────────────────────────
    aberturas = (p['aberturas'] or 0) + 1
    primeira  = p['status'] == 'enviado'
    if USE_PG:
        db_exec("UPDATE pdfs SET aberturas=%s,aberto_em=%s,status=%s WHERE token=%s",
            (aberturas, now_str(), 'aberto', token))
    else:
        db_exec("UPDATE pdfs SET aberturas=?,aberto_em=?,status=? WHERE token=?",
            (aberturas, now_str(), 'aberto', token))
    hora = agora.strftime('%H:%M')
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
    notificar(cfg, txt, html_notif)
    arquivo  = bytes(p['arquivo']) if p['arquivo'] else b''
    filename = (p['filename'] or 'orcamento.pdf').replace('"', '')
    return Response(arquivo, mimetype='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{filename}"',
                 'Cache-Control': 'no-store'})

@app.route('/renovar_link/<int:id>', methods=['POST'])
@login_required
def renovar_link(id):
    """Gera novo token e reseta o prazo de 48h do PDF."""
    novo_token = str(uuid.uuid4()).replace('-','')[:16]
    sql = 'UPDATE pdfs SET token=%s, criado_em=%s, status=%s, aberturas=0, aberto_em=NULL WHERE id=%s' if USE_PG else \
          'UPDATE pdfs SET token=?, criado_em=?, status=?, aberturas=0, aberto_em=NULL WHERE id=?'
    db_exec(sql, (novo_token, now_str(), 'enviado', id))
    link = f"{get_base_url()}/pdf/{novo_token}"
    return jsonify({'ok': True, 'token': novo_token, 'link': link})

@app.route('/atualizar_status_pdf/<int:id>', methods=['POST'])
@login_required
def atualizar_status_pdf(id):
    d = request.get_json()
    status = d.get('status','')
    if status not in ['enviado','aberto','fechou','negociando','perdido']:
        return jsonify({'ok': False})
    sql = 'UPDATE pdfs SET status=%s WHERE id=%s' if USE_PG else 'UPDATE pdfs SET status=? WHERE id=?'
    db_exec(sql, (status, id))
    return jsonify({'ok': True})

@app.route('/deletar_pdf/<int:id>', methods=['POST'])
@login_required
def deletar_pdf(id):
    sql = 'DELETE FROM pdfs WHERE id=%s' if USE_PG else 'DELETE FROM pdfs WHERE id=?'
    db_exec(sql, (id,))
    return redirect('/')

@app.route('/deletar_pdf_ajax/<int:id>', methods=['POST'])
@login_required
def deletar_pdf_ajax(id):
    sql = 'DELETE FROM pdfs WHERE id=%s' if USE_PG else 'DELETE FROM pdfs WHERE id=?'
    db_exec(sql, (id,))
    return jsonify({'ok': True})

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
    if request.method == 'POST':
        save_config(request.get_json())
        return jsonify({'ok': True})
    return render_template('configuracoes.html', cfg=get_config())

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
    email, senha = d.get('email','').strip(), d.get('senha','').strip()
    if not email or not senha:
        return jsonify({'ok': False, 'erro': 'Preencha email e senha'})
    html = '<div style="font-family:sans-serif;padding:2rem"><h2 style="color:#16a34a">✅ Email funcionando!</h2><p>Notificações do app de Orçamentos configuradas com sucesso.</p></div>'
    ok, erro = notificar_email(email, senha, html)
    return jsonify({'ok': ok, 'erro': erro})

if __name__ == '__main__':
    init_db()
    print('\n✅ App de Orçamentos iniciado!')
    print('👉 Acesse: http://localhost:5000\n')
    app.run(debug=False, port=5000)

# Para produção (gunicorn)
init_db()
