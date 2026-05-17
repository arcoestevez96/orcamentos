import os, json, uuid, requests, smtplib
from datetime import datetime
from flask import Flask, render_template, request, redirect, jsonify, send_file, Response
from werkzeug.utils import secure_filename
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import io

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

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
                criado_em TEXT NOT NULL, aberto_em TEXT, aberturas INTEGER DEFAULT 0
            )''')
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

def save_config(dados):
    for k, v in dados.items():
        if USE_PG:
            db_exec('UPDATE config SET valor=%s WHERE chave=%s', (v, k))
        else:
            db_exec('UPDATE config SET valor=? WHERE chave=?', (v, k))

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
        with smtplib.SMTP('smtp.gmail.com', 587) as s:
            s.starttls()
            s.login(email, senha)
            s.sendmail(email, email, msg.as_string())
        return True, 'ok'
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

def notificar(cfg, txt, html=None):
    if cfg.get('whatsapp_numero') and cfg.get('whatsapp_apikey'):
        notificar_whatsapp(cfg['whatsapp_numero'], cfg['whatsapp_apikey'], txt)
    if cfg.get('email_remetente') and cfg.get('email_senha_app'):
        notificar_email(cfg['email_remetente'], cfg['email_senha_app'], html or f'<p>{txt}</p>')
    if cfg.get('telegram_token') and cfg.get('telegram_chat_id'):
        notificar_telegram(cfg['telegram_token'], cfg['telegram_chat_id'], txt)
    if cfg.get('zapi_instance') and cfg.get('zapi_token') and cfg.get('zapi_client_token') and cfg.get('zapi_phone'):
        notificar_zapi(cfg['zapi_instance'], cfg['zapi_token'], cfg['zapi_client_token'], cfg['zapi_phone'], txt)

# ── helpers ──────────────────────────────────────────────────────────────────

def calcular_total(itens):
    total = 0
    for etapa in itens:
        for item in etapa.get('itens', []):
            try: total += float(item.get('quantidade', 0)) * float(item.get('preco', 0))
            except: pass
    return total

def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ── rotas ────────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    orcamentos = db_exec('SELECT * FROM orcamentos ORDER BY criado_em DESC', fetch='all') or []
    lista = [{**o, 'total': calcular_total(json.loads(o['itens']))} for o in orcamentos]
    return render_template('dashboard.html', orcamentos=lista)

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
        hora = datetime.now().strftime('%H:%M')
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

@app.route('/link/<token>')
def gerar_link(token):
    return jsonify({'link': f"{get_base_url()}/ver/{token}"})

# ── PDFs ─────────────────────────────────────────────────────────────────────

@app.route('/pdfs')
def pdfs():
    rows = db_exec('SELECT id,token,cliente_nome,cliente_telefone,titulo,filename,status,criado_em,aberto_em,aberturas FROM pdfs ORDER BY criado_em DESC', fetch='all') or []
    return render_template('pdfs.html', pdfs=rows)

@app.route('/upload_pdf', methods=['POST'])
def upload_pdf():
    if 'pdf' not in request.files:
        return jsonify({'ok': False, 'erro': 'Nenhum arquivo enviado'})
    f = request.files['pdf']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'erro': 'Apenas PDFs são aceitos'})
    dados = f.read()
    token = str(uuid.uuid4()).replace('-','')[:16]
    filename = secure_filename(f.filename)
    if USE_PG:
        import psycopg2
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,titulo,arquivo,filename,status,criado_em) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             request.form.get('titulo', filename), psycopg2.Binary(dados), filename, 'enviado', now_str()))
    else:
        db_exec('INSERT INTO pdfs(token,cliente_nome,cliente_telefone,titulo,arquivo,filename,status,criado_em) VALUES(?,?,?,?,?,?,?,?)',
            (token, request.form.get('cliente_nome','Cliente'), request.form.get('cliente_telefone',''),
             request.form.get('titulo', filename), dados, filename, 'enviado', now_str()))
    return jsonify({'ok': True, 'token': token, 'link': f"{get_base_url()}/pdf/{token}"})

@app.route('/pdf/<token>')
def ver_pdf(token):
    sql = 'SELECT * FROM pdfs WHERE token=%s' if USE_PG else 'SELECT * FROM pdfs WHERE token=?'
    p = db_exec(sql, (token,), fetch='one')
    if not p: return 'PDF não encontrado.', 404
    cfg = get_config()
    aberturas = (p['aberturas'] or 0) + 1
    primeira = p['status'] == 'enviado'
    if USE_PG:
        db_exec("UPDATE pdfs SET aberturas=%s,aberto_em=%s,status=%s WHERE token=%s",
            (aberturas, now_str(), 'aberto', token))
    else:
        db_exec("UPDATE pdfs SET aberturas=?,aberto_em=?,status=? WHERE token=?",
            (aberturas, now_str(), 'aberto', token))
    hora = datetime.now().strftime('%H:%M')
    if primeira:
        txt = f"👁 {p['cliente_nome']} abriu o PDF {p['titulo']} pela 1ª vez às {hora}!"
        html = f"""<div style="font-family:sans-serif;padding:2rem;background:#f0fdf4;border-radius:12px">
            <h2 style="color:#16a34a">👁 PDF Visualizado!</h2>
            <p><strong>{p['cliente_nome']}</strong> abriu <strong>{p['titulo']}</strong> às <strong>{hora}</strong>.</p></div>"""
    else:
        txt = f"🔄 {p['cliente_nome']} abriu novamente {p['titulo']} às {hora}. Total: {aberturas}x"
        html = f"""<div style="font-family:sans-serif;padding:2rem;background:#fffbeb;border-radius:12px">
            <h2 style="color:#f59e0b">🔄 PDF Aberto Novamente</h2>
            <p><strong>{p['cliente_nome']}</strong> abriu <strong>{p['titulo']}</strong> às <strong>{hora}</strong>. Total: <strong>{aberturas}x</strong></p></div>"""
    notificar(cfg, txt, html)
    arquivo = bytes(p['arquivo']) if p['arquivo'] else b''
    return Response(arquivo, mimetype='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{p["filename"]}"'})

@app.route('/deletar_pdf/<int:id>', methods=['POST'])
def deletar_pdf(id):
    sql = 'DELETE FROM pdfs WHERE id=%s' if USE_PG else 'DELETE FROM pdfs WHERE id=?'
    db_exec(sql, (id,))
    return redirect('/pdfs')

# ── configurações ─────────────────────────────────────────────────────────────

@app.route('/configuracoes', methods=['GET', 'POST'])
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
