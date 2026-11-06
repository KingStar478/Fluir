# Importacao das bibliotecas usadas no sistema Flask
import re
import hmac
import hashlib
import sqlite3
import time
import json
import os
import secrets
import subprocess
import socket
import threading
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen
from uuid import uuid4

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from models import Agendamento, HistoricoGerenciamento, LeituraNotificacaoCalendario, NotificacaoCalendario, Porto, User, db

# Criacao da aplicacao Flask
app = Flask(__name__)

# Configuracao do caminho do banco de dados SQLite
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "usuarios.db"
DB_PATH.parent.mkdir(exist_ok=True)
CHAT_BASE_URL = os.getenv("FLUIR_CHAT_URL", "http://127.0.0.1:8001").rstrip("/")
CHAT_BUILD_ID = hashlib.sha256((BASE_DIR / "chat_fastapi.py").read_bytes()).hexdigest()[:16]
_chat_start_lock = threading.Lock()
_chat_server_process = None


def email_base_por_nome(nome):
    partes = [
        parte for parte in unicodedata.normalize("NFKD", nome or "")
        if not unicodedata.combining(parte)
    ]
    texto = "".join(partes)
    nomes = [parte for parte in re.findall(r"[a-zA-Z0-9]+", texto) if parte.lower() not in {"de", "da", "do", "das", "dos", "e"}]
    if not nomes:
        nomes = ["F"]
    iniciais = (nomes[0][0] + (nomes[-1][0] if len(nomes) > 1 else nomes[0][0])).upper()
    return f"{iniciais}@fluir.com"


def email_unico_por_nome(nome, ignorar_id=None):
    base = email_base_por_nome(nome)
    prefixo = base[:-len("@fluir.com")]
    candidato, sufixo = base, 2
    while User.query.filter(db.func.lower(User.email) == candidato.lower(), User.id != (ignorar_id or "")).first():
        candidato = f"{prefixo}{sufixo}@fluir.com"
        sufixo += 1
    return candidato


def email_valido_e_disponivel(email, ignorar_id=None):
    email = (email or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return None
    existente = User.query.filter(db.func.lower(User.email) == email, User.id != (ignorar_id or "")).first()
    return None if existente else email

# Configuracao geral da aplicacao
def carregar_segredo_local():
    configurado = os.environ.get("FLUIR_SECRET_KEY")
    if configurado:
        return configurado
    caminho = BASE_DIR / "instance" / "fluir.secret"
    caminho.parent.mkdir(exist_ok=True)
    try:
        return caminho.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        segredo = secrets.token_hex(32)
        try:
            with caminho.open("x", encoding="utf-8") as arquivo:
                arquivo.write(segredo)
        except FileExistsError:
            return caminho.read_text(encoding="utf-8").strip()
        return segredo


app.config["SECRET_KEY"] = carregar_segredo_local()
os.environ["FLUIR_SECRET_KEY"] = app.config["SECRET_KEY"]
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH.as_posix()}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
MAX_IMAGE_FILE_SIZE = 5 * 1024 * 1024

# Inicializacao do banco pelo SQLAlchemy
db.init_app(app)


# Atualizacao leve do SQLite quando novos campos sao adicionados
def garantir_colunas_agendamento():
    colunas = {
        "tipo_navio": "VARCHAR(120)",
        "fcl_lcl": "VARCHAR(20)",
        "temperatura": "VARCHAR(80)",
        "capacidade_navio": "VARCHAR(120)",
        "plano_carga": "TEXT",
        "eta": "DATETIME",
        "etb": "DATETIME",
        "ets": "DATETIME",
        "verificar_saida_adiada_ate": "DATETIME",
        "concluido_em": "DATETIME",
        "nome_embarcacao": "VARCHAR(160)",
        "numero_viagem": "VARCHAR(80)",
        "armador": "VARCHAR(160)",
        "calado": "VARCHAR(80)",
        "gate_cutoff": "DATETIME",
        "cargo_doc_cutoff": "DATETIME",
        "portos_escala": "TEXT",
    }
    with sqlite3.connect(DB_PATH) as conn:
        existentes = {
            coluna[1]
            for coluna in conn.execute("PRAGMA table_info(agendamentos)").fetchall()
        }
        for nome, tipo in colunas.items():
            if nome not in existentes:
                conn.execute(f"ALTER TABLE agendamentos ADD COLUMN {nome} {tipo}")


def garantir_colunas_acesso():
    tabelas = {
        "users": {
            "identificacao_interna": "VARCHAR(20)",
            "deve_trocar_senha": "BOOLEAN NOT NULL DEFAULT 0",
            "cargo": "VARCHAR(120)",
            "poder_maior": "BOOLEAN NOT NULL DEFAULT 0",
            "email": "VARCHAR(254)",
            "avatar_path": "VARCHAR(255)",
            "created_at": "DATETIME",
            "last_access_at": "DATETIME",
            "status_aprovacao": "VARCHAR(20) NOT NULL DEFAULT 'aprovada'",
        },
        "portos": {
            "codigo_poder_maior": "VARCHAR(20)",
        },
    }
    with sqlite3.connect(DB_PATH) as conn:
        for tabela, colunas in tabelas.items():
            existentes = {
                coluna[1]
                for coluna in conn.execute(f"PRAGMA table_info({tabela})").fetchall()
            }
            for nome, tipo in colunas.items():
                if nome not in existentes:
                    conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
        audit_columns = {column[1] for column in conn.execute("PRAGMA table_info(historico_gerenciamento)").fetchall()}
        if audit_columns and "codigo_porto" not in audit_columns:
            conn.execute("ALTER TABLE historico_gerenciamento ADD COLUMN codigo_porto VARCHAR(20) NOT NULL DEFAULT ''")
        conn.execute(
            """
            UPDATE users
            SET cargo = ?
            WHERE cargo IS NULL OR TRIM(cargo) = ''
            """,
            ("Administracao",),
        )
        conn.execute(
            "UPDATE users SET status_aprovacao = 'aprovada' "
            "WHERE status_aprovacao IS NULL OR TRIM(status_aprovacao) = ''"
        )
        conn.execute(
            "UPDATE users SET identificacao_interna = upper(substr(id, 1, 8)) "
            "WHERE identificacao_interna IS NULL OR TRIM(identificacao_interna) = ''"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_identificacao_interna_unique "
            "ON users(identificacao_interna)"
        )
        usuarios = conn.execute("SELECT id, nome_completo, email FROM users ORDER BY id").fetchall()
        emails_reservados = {
            (email or "").strip().casefold()
            for _, _, email in usuarios
            if email and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip())
        }
        emails_usados = set()
        for user_id, nome, email in usuarios:
            email_atual = (email or "").strip()
            chave_email = email_atual.casefold()
            email_valido = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email_atual))
            if email_valido and chave_email not in emails_usados:
                emails_usados.add(chave_email)
                continue

            base = email_base_por_nome(nome)
            prefixo = base[:-len("@fluir.com")]
            candidate, sufixo = base, 2
            while candidate.casefold() in emails_reservados or candidate.casefold() in emails_usados:
                candidate = f"{prefixo}{sufixo}@fluir.com"
                sufixo += 1
            emails_reservados.add(candidate.casefold())
            emails_usados.add(candidate.casefold())
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (candidate, user_id))
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email_unique "
            "ON users(email) WHERE email IS NOT NULL AND email != ''"
        )


# Criacao automatica das tabelas cadastradas nos models
with app.app_context():
    db.create_all()
    garantir_colunas_acesso()
    garantir_colunas_agendamento()


# Itens do menu lateral do sistema
NAV_ITEMS = [
    ("Painel", "home"),
    ("Calendário", "calendario"),
    ("Chat", "chat_live"),
]

NAV_GERENCIAMENTO = ("Funcionários", "gerenciar_funcionarios")

AVATAR_DIR = BASE_DIR / "static" / "uploads" / "avatars"
APPEAL_DIR = BASE_DIR / "instance" / "contestacoes"
ALLOWED_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Pontos reais usados no mapa logistico
MAPA_PONTOS = [
    {
        "nome": "Porto de Belém",
        "lat": -1.43300000,
        "lon": -48.49900000,
        "cidade": "Belém, PA",
        "hidrovia": "Baía do Guajará / Rio Pará",
        "fonte": "Wilhelmsen Port Services",
    },
    {
        "nome": "Porto de Santarém",
        "lat": -2.41580000,
        "lon": -54.72900000,
        "cidade": "Santarém, PA",
        "hidrovia": "Rio Tapajós / Rio Amazonas",
        "fonte": "Wilhelmsen Port Services",
    },
    {
        "nome": "Porto de Manaus",
        "lat": -3.14851100,
        "lon": -60.03247700,
        "cidade": "Manaus, AM",
        "hidrovia": "Rio Negro / Rio Amazonas",
        "fonte": "Wilhelmsen Port Services",
    },
    {
        "nome": "Porto de Itacoatiara",
        "lat": -3.13502100,
        "lon": -58.43854400,
        "cidade": "Itacoatiara, AM",
        "hidrovia": "Rio Amazonas",
        "fonte": "Wilhelmsen Port Services",
    },
    {
        "nome": "Porto de Santana",
        "lat": -0.05850000,
        "lon": -51.17050000,
        "cidade": "Santana, AP",
        "hidrovia": "Rio Amazonas",
        "fonte": "MarineLink Ports Directory",
    },
]

# Dados principais do porto usado como exemplo do sistema
NOSSO_PORTO = {
    "codigo": "BR123",
    "nome": "Porto de Manaus",
    "lat": -3.14851100,
    "lon": -60.03247700,
    "cidade": "Manaus, AM",
    "hidrovia": "Rio Negro / Rio Amazonas",
    "fonte": "Wilhelmsen Port Services",
}

# Fluxos fixos exibidos no resumo operacional da Home
FLUXOS_PORTUARIOS = [
    {
        "tipo": "saida",
        "titulo": "Devemos levar para o Porto de Santarém",
        "porto": "Porto de Santarém",
        "hidrovia": "Rio Tapajós / Rio Amazonas",
        "carga": "Medicamentos e insumos básicos",
        "prioridade": "Essencial",
        "previsao": "22/05/2026 às 13:00",
        "status": "Aguardando janela de embarque",
    },
    {
        "tipo": "entrada",
        "titulo": "Porto de Belém está mandando para cá",
        "porto": "Porto de Belém",
        "hidrovia": "Baía do Guajará / Rio Pará",
        "carga": "Alimentos não perecíveis",
        "prioridade": "Alta",
        "previsao": "23/05/2026 às 09:40",
        "status": "Em trânsito fluvial",
    },
    {
        "tipo": "saida",
        "titulo": "Devemos levar para o Porto de Itacoatiara",
        "porto": "Porto de Itacoatiara",
        "hidrovia": "Rio Amazonas",
        "carga": "Carga geral e equipamentos portuários",
        "prioridade": "Operacional",
        "previsao": "24/05/2026 às 16:15",
        "status": "Fila de atracação em organização",
    },
    {
        "tipo": "entrada",
        "titulo": "Porto de Santana está mandando para cá",
        "porto": "Porto de Santana",
        "hidrovia": "Rio Amazonas",
        "carga": "Insumos básicos",
        "prioridade": "Essencial",
        "previsao": "25/05/2026 às 11:30",
        "status": "Previsão confirmada",
    },
]

# Legenda de cores e tipos de eventos do calendario
CALENDARIO_CORES = {
    "atracacao": {
        "label": "Atracação",
        "descricao": "Chegada ou atracação de embarcações fluviais no porto.",
    },
    "frota": {
        "label": "Frota",
        "descricao": "Check-ins, disponibilidade e movimentações da frota.",
    },
    "inspecao": {
        "label": "Inspeção",
        "descricao": "Vistorias, conferências e inspeções operacionais.",
    },
    "saida": {
        "label": "Saída",
        "descricao": "Saídas confirmadas ou previstas de embarcações fluviais.",
    },
    "carga": {
        "label": "Carga",
        "descricao": "Operações de carga, descarga e movimentação.",
    },
    "embarque": {
        "label": "Embarque",
        "descricao": "Janelas e períodos reservados para embarque.",
    },
    "manutencao": {
        "label": "Manutenção",
        "descricao": "Manutenções de cais, pátios e estruturas operacionais.",
    },
}

# Legenda atual de cores por tipo de carga do calendario
CALENDARIO_CORES = {
    "conteinerizada": {
        "label": "Carga Conteinerizada",
        "descricao": "Mercadorias variadas embaladas e lacradas em contêineres padronizados.",
        "navio": "Porta-Contêineres",
    },
    "granel": {
        "label": "Carga a Granel",
        "descricao": "Produtos em massa, sem embalagem, incluindo granel sólido, líquido e gases.",
        "navio": "Graneleiros, Petroleiros, Navios-Tanque ou Gaseiros",
    },
    "geral": {
        "label": "Carga Geral",
        "descricao": "Mercadorias soltas, caixas, fardos, tambores, sacos ou cargas de projeto.",
        "navio": "Carga Geral ou Polivalentes",
    },
    "rolante": {
        "label": "Carga Rolante",
        "descricao": "Veiculos e cargas sob rodas que embarcam e desembarcam por rampas.",
        "navio": "Ro-Ro",
    },
    "frigorifica": {
        "label": "Carga Frigorífica",
        "descricao": "Produtos perecíveis que exigem controle rigoroso de temperatura.",
        "navio": "Frigoríficos ou contêineres Reefer",
    },
    "viva": {
        "label": "Carga Viva",
        "descricao": "Animais vivos que exigem ventilação, alimentação e espaço durante a viagem.",
        "navio": "Navios boiadeiros ou embarcações adaptadas para animais vivos",
    },
}

# Mapeamento dos meses para navegacao no calendario
MESES = {
    "Janeiro": 1,
    "Fevereiro": 2,
    "Março": 3,
    "Abril": 4,
    "Maio": 5,
    "Junho": 6,
    "Julho": 7,
    "Agosto": 8,
    "Setembro": 9,
    "Outubro": 10,
    "Novembro": 11,
    "Dezembro": 12,
}

# Padroes fracos bloqueados na validacao de senha
PADROES_BLOQUEADOS = ["123456", "654321", "abcdef", "qwerty", "asdfgh", "zxcvbn"]

CARGOS_FUNCIONARIO = [
    "Operacoes Portuarias",
    "Navegacao e Trafego Fluvial",
    "Seguranca do Trabalho",
    "Meio Ambiente",
    "Logistica",
    "Embarque",
    "Fiscalizacao e Controle",
    "Administracao",
]
CARGOS_EXIBICAO = {
    "Operacoes Portuarias": "Operações Portuárias",
    "Navegacao e Trafego Fluvial": "Navegação e Tráfego Fluvial",
    "Seguranca do Trabalho": "Segurança do Trabalho",
    "Meio Ambiente": "Meio Ambiente",
    "Logistica": "Logística",
    "Embarque": "Embarque",
    "Fiscalizacao e Controle": "Fiscalização e Controle",
    "Administracao": "Administração",
}


def cargo_exibicao(cargo):
    return CARGOS_EXIBICAO.get(cargo or "", cargo or "Não informado")


# Validacao de forca da senha do usuario
def validar_senha(password):
    erros = []

    if len(password) < 8:
        erros.append("A senha deve ter pelo menos 8 caracteres.")
    if not re.search(r"[A-Z]", password):
        erros.append("A senha deve ter pelo menos 1 letra maiúscula.")
    if not re.search(r"[a-z]", password):
        erros.append("A senha deve ter pelo menos 1 letra minúscula.")
    if not re.search(r"\d", password):
        erros.append("A senha deve ter pelo menos 1 número.")
    if not re.search(r"[@#$%!]", password):
        erros.append("A senha deve ter pelo menos 1 caractere especial (@, #, $, %, !).")

    return erros


def senha_esta_protegida(senha_salva):
    return (senha_salva or "").startswith(("scrypt:", "pbkdf2:"))


def conferir_senha(senha_salva, senha_digitada):
    if senha_esta_protegida(senha_salva):
        return check_password_hash(senha_salva, senha_digitada)
    return hmac.compare_digest(senha_salva or "", senha_digitada or "")


# Remocao de acentos para comparacoes de texto
def remover_acentos(valor):
    return "".join(
        caractere
        for caractere in unicodedata.normalize("NFKD", valor)
        if not unicodedata.combining(caractere)
    )


# Normalizacao de texto para validacoes
def normalizar_texto(valor):
    valor = remover_acentos(valor or "").lower()
    return re.sub(r"[^a-z0-9]", "", valor)


# Normalizacao do codigo portuario digitado
def normalizar_codigo_porto(valor):
    return re.sub(r"[^A-Za-z0-9]", "", valor or "").upper()[:5]


# Geracao de avisos sobre senhas inseguras
def avisos_senha(senha, usuario, nome_completo, nascimento):
    avisos = []
    senha_normalizada = normalizar_texto(senha)
    usuario_normalizado = normalizar_texto(usuario)

    if usuario_normalizado and len(usuario_normalizado) >= 3 and usuario_normalizado in senha_normalizada:
        avisos.append("Não é seguro usar seu e-mail como senha, tem certeza desta senha?")

    nome_normalizado = normalizar_texto(nome_completo)
    partes_nome = [
        normalizar_texto(parte)
        for parte in (nome_completo or "").split()
        if len(normalizar_texto(parte)) >= 3
    ]
    if nome_normalizado and len(nome_normalizado) >= 4 and nome_normalizado in senha_normalizada:
        avisos.append("Não é seguro usar seu nome completo, tem certeza desta senha?")
    elif any(parte in senha_normalizada for parte in partes_nome):
        avisos.append("Não é seguro usar seu nome completo, tem certeza desta senha?")

    nascimento_digitos = re.sub(r"\D", "", nascimento or "")
    combinacoes_data = {nascimento_digitos, nascimento_digitos[:4], nascimento_digitos[-4:]}
    combinacoes_data.discard("")
    senha_digitos = re.sub(r"\D", "", senha or "")
    if any(combinacao in senha_digitos for combinacao in combinacoes_data):
        avisos.append("Não é seguro usar sua data de aniversário, certeza desta senha?")

    if any(padrao in senha_normalizada for padrao in PADROES_BLOQUEADOS):
        avisos.append("Não é seguro usar sequências simples como 123456, abcdef ou qwerty.")

    return list(dict.fromkeys(avisos))


# Leitura dos dados enviados por formulario ou JSON
def get_payload():
    if request.is_json:
        return request.get_json() or {}
    return request.form


# Verificacao se a resposta deve ser JSON
def wants_json():
    return request.is_json or request.path.startswith("/api/")


# Formatacao da data de nascimento para exibicao
def formatar_data(valor):
    if not valor:
        return ""
    try:
        return date.fromisoformat(valor).strftime("%d/%m/%Y")
    except ValueError:
        return valor


def formatar_data_input(valor):
    if not valor:
        return ""
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(valor, formato).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""

# Cadastro e validacao de funcionario
def cadastro_funcionario_payload(data):
    nome = (data.get("nome_completo") or "").strip()
    nascimento = formatar_data((data.get("data_nascimento") or "").strip())
    password = (data.get("password") or "").strip()
    email = email_unico_por_nome(nome)
    cargo = (data.get("cargo") or "").strip()
    codigo_porto = normalizar_codigo_porto(data.get("codigo_porto"))
    codigo_gestao = normalizar_codigo_porto(data.get("codigo_gestao"))

    if not all([nome, nascimento, password, cargo, codigo_porto]):
        return {"error": "Preencha todos os campos"}, 400

    if cargo not in CARGOS_FUNCIONARIO:
        return {"error": "Cargo inválido."}, 400

    erros_senha = validar_senha(password)
    if erros_senha:
        return {"error": erros_senha[0], "errors": erros_senha}, 400

    porto = Porto.query.filter_by(codigo_portuario=codigo_porto).first()
    poder_maior = False
    if not porto:
        return {"error": "Código portuário não cadastrado."}, 400
    if codigo_gestao:
        poder_maior = porto.codigo_poder_maior == codigo_gestao
        if not poder_maior:
            return {"error": "Código de Gestão incorreto para este porto."}, 400

    local = unicodedata.normalize("NFKD", email.split("@", 1)[0]).encode("ascii", "ignore").decode("ascii").lower()
    username_base = re.sub(r"[^a-z0-9._-]", "", local)[:48] or "funcionario"
    username = username_base
    while User.query.filter_by(username=username).first():
        username = f"{username_base[:38]}-{uuid4().hex[:8]}"

    novo = User(
        nome_completo=nome,
        data_nascimento=nascimento,
        username=username,
        password=generate_password_hash(password),
        email=email,
        codigo_porto=porto.codigo_portuario,
        cargo=cargo,
        poder_maior=poder_maior,
        status_aprovacao="pendente",
    )

    try:
        db.session.add(novo)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return {"error": "Erro ao salvar o usuário."}, 500

    return {"msg": "Solicitação enviada. O acesso será liberado após a aprovação da Fiscalização e Controle."}, 201

# Cadastro e validacao de porto e Codigo de Gestao
def cadastro_porto_payload(data):
    nome_porto = (data.get("nome_porto") or "").strip()
    nome_rio = (data.get("nome_rio") or "").strip()
    codigo_portuario = normalizar_codigo_porto(data.get("codigo_portuario"))
    codigo_poder_maior = normalizar_codigo_porto(data.get("codigo_poder_maior"))
    cidade = (data.get("cidade") or "").strip()
    estado = (data.get("estado") or "").strip()
    latitude = (data.get("latitude") or "").strip()
    longitude = (data.get("longitude") or "").strip()
    ponto_quilometrico = (data.get("ponto_quilometrico") or "").strip()
    margem = (data.get("margem") or "").strip()
    tipo_porto = (data.get("tipo_porto") or "").strip()

    campos = [
        nome_porto,
        nome_rio,
        codigo_portuario,
        codigo_poder_maior,
        cidade,
        estado,
        latitude,
        longitude,
        ponto_quilometrico,
        margem,
        tipo_porto,
    ]
    if not all(campos):
        return {"error": "Preencha todos os campos do porto"}, 400

    if Porto.query.filter_by(codigo_portuario=codigo_portuario).first():
        return {"error": "Codigo portuario ja cadastrado"}, 400

    if Porto.query.filter_by(codigo_poder_maior=codigo_poder_maior).first():
        return {"error": "Codigo de Gestao ja cadastrado"}, 400

    if codigo_poder_maior == codigo_portuario:
        return {"error": "O Codigo de Gestao deve ser diferente do Codigo Portuario"}, 400

    try:
        novo_porto = Porto(
            nome_porto=nome_porto,
            nome_rio=nome_rio,
            codigo_portuario=codigo_portuario,
            codigo_poder_maior=codigo_poder_maior,
            cidade=cidade,
            estado=estado,
            latitude=latitude,
            longitude=longitude,
            ponto_quilometrico=ponto_quilometrico,
            margem=margem,
            tipo_porto=tipo_porto,
            administrador_user_id="",
            administrador_username="",
        )
        db.session.add(novo_porto)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return {"error": "Erro ao salvar porto"}, 500

    return {"msg": "Porto cadastrado com sucesso!"}, 201

# Protecao de paginas que exigem login
def login_required():
    if not session.get("logado"):
        flash("Acesso negado. Faça login para continuar.", "error")
        return redirect(url_for("login"))
    user = usuario_logado()
    if not user:
        session.clear()
        flash("Não foi possível localizar esta conta. Faça login novamente.", "error")
        return redirect(url_for("login"))
    if user.status_aprovacao != "aprovada":
        session.clear()
        status = "aguarda aprovação" if user.status_aprovacao == "pendente" else "foi rejeitada"
        flash(f"Esta conta {status}. Entre em contato com a Fiscalização e Controle.", "error")
        return redirect(url_for("login"))
    if user.deve_trocar_senha:
        session.pop("logado", None)
        session["senha_troca_pendente_id"] = user.id
        return redirect(url_for("trocar_senha_obrigatoria"))
    return None


# Verificacao se o usuario logado tem Codigo de Gestao do porto
def usuario_e_administrador_portuario():
    username = session.get("usuario")
    codigo_porto = session.get("porto")
    if not username or not codigo_porto:
        return False
    usuario = User.query.filter_by(username=username, codigo_porto=codigo_porto).first()
    return bool(usuario and usuario.poder_maior)


# Consulta do usuario logado no porto atual
def usuario_logado():
    username = session.get("usuario")
    codigo_porto = session.get("porto")
    if not username or not codigo_porto:
        return None
    return User.query.filter_by(username=username, codigo_porto=codigo_porto).first()


def usuario_e_fiscalizacao(usuario):
    return bool(usuario and remover_acentos(usuario.cargo or "").strip().lower() == "fiscalizacao e controle")


def registrar_acao_funcionario(ator, funcionario, acao, detalhes=""):
    db.session.add(HistoricoGerenciamento(
        ator_id=ator.id,
        ator_nome=ator.nome_completo,
        funcionario_id=funcionario.id,
        funcionario_nome=funcionario.nome_completo,
        codigo_porto=ator.codigo_porto,
        acao=acao,
        detalhes=detalhes,
        criado_em=datetime.now(),
    ))


def obter_autorizacao_gerenciamento(representante=False):
    ator = usuario_logado()
    if not ator or ator.status_aprovacao != "aprovada" or not usuario_e_fiscalizacao(ator):
        abort(403)
    if representante and not ator.poder_maior:
        abort(403)
    return ator


# Conversao de setor para identificador seguro de sala
def setor_para_sala(setor):
    setor = normalizar_texto(setor or "geral")
    return setor or "geral"


# Assinatura simples para impedir alteracao manual do usuario/setor do chat
def gerar_token_chat(usuario, porto, setor):
    payload = f"{usuario}|{porto}|{setor}".encode("utf-8")
    segredo = app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(segredo, payload, hashlib.sha256).hexdigest()


# Registro de aviso para outros usuarios quando o calendario muda
def registrar_notificacao_calendario(agendamento):
    usuario = usuario_logado()
    setor = usuario.cargo if usuario and usuario.cargo else "Setor nao informado"
    titulo = "Calendário atualizado"
    mensagem = (
        f"{session.get('usuario')} ({setor}) cadastrou {agendamento.nome_embarcacao} "
        f"com ETA em {formatar_data_hora(agendamento.eta or agendamento.previsao)}."
    )
    notificacao = NotificacaoCalendario(
        codigo_porto=session.get("porto"),
        titulo=titulo,
        mensagem=mensagem,
        criado_por=session.get("usuario"),
        criado_em=datetime.now(),
    )
    db.session.add(notificacao)


# Listagem das notificacoes exibidas na Home para outros usuarios
def carregar_notificacoes_home():
    return (
        db.session.query(NotificacaoCalendario)
        .outerjoin(
            LeituraNotificacaoCalendario,
            (LeituraNotificacaoCalendario.notificacao_id == NotificacaoCalendario.id)
            & (LeituraNotificacaoCalendario.usuario == session.get("usuario")),
        )
        .filter(
            NotificacaoCalendario.codigo_porto == session.get("porto"),
            NotificacaoCalendario.criado_por != session.get("usuario"),
            LeituraNotificacaoCalendario.id.is_(None),
        )
        .order_by(NotificacaoCalendario.criado_em.desc())
        .limit(5)
        .all()
    )


@app.route("/notificacoes/calendario/marcar-lidas", methods=["POST"])
def marcar_notificacoes_calendario_lidas():
    blocked = login_required()
    if blocked:
        return blocked
    usuario = session.get("usuario")
    notificacoes = carregar_notificacoes_home()
    for notificacao in notificacoes:
        db.session.add(LeituraNotificacaoCalendario(notificacao_id=notificacao.id, usuario=usuario))
    db.session.commit()
    return redirect(url_for("home"))


# Conversao do tipo de agendamento em classe visual do calendario
def tipo_para_classe(tipo_agendamento):
    tipo = normalizar_texto(tipo_agendamento)
    mapa = {
        "atracacao": "atracacao",
        "saida": "saida",
        "retirada": "saida",
        "carga": "carga",
        "embarque": "embarque",
        "inspecao": "inspecao",
        "manutencao": "manutencao",
    }
    return mapa.get(tipo, "carga")


# Conversao do tipo de carga em classe visual do calendario
def carga_para_classe(tipo_carga):
    tipo = normalizar_texto(tipo_carga)
    mapa = {
        "cargaconteinerizada": "conteinerizada",
        "conteinerizada": "conteinerizada",
        "conteineres": "conteinerizada",
        "containers": "conteinerizada",
        "cargaagranel": "granel",
        "granel": "granel",
        "granelsolido": "granel",
        "granelliquido": "granel",
        "gases": "granel",
        "cargageral": "geral",
        "breakbulk": "geral",
        "cargarolante": "rolante",
        "ro-ro": "rolante",
        "roro": "rolante",
        "cargafrigorifica": "frigorifica",
        "cargafrigorfica": "frigorifica",
        "frigorifica": "frigorifica",
        "frigorfica": "frigorifica",
        "reefer": "frigorifica",
        "cargaviva": "viva",
        "viva": "viva",
    }
    return mapa.get(tipo, "geral")


# Formatacao de datas operacionais opcionais
def formatar_data_hora(valor):
    if not valor:
        return ""
    return valor.strftime("%d/%m/%Y às %H:%M")


# Definicao do fluxo operacional conforme o tipo de agendamento
def fluxo_por_tipo_agendamento(tipo_agendamento):
    tipo = normalizar_texto(tipo_agendamento)
    if tipo in ["saida", "retirada", "embarque"]:
        return "Levada"
    if tipo in ["atracacao", "carga"]:
        return "Trazida"
    return "Operacional"


# Calculo do status automatico com base na previsao
def status_por_previsao(previsao):
    agora = datetime.now()
    if previsao < agora:
        return "Atrasado"
    if previsao.date() == agora.date():
        return "Previsto para hoje"
    return "Agendado"


def passagem_pelo_porto_concluida(agendamento):
    """Uma passagem só entra no histórico após confirmação explícita de saída."""
    status = (agendamento.status or "").strip().casefold()
    return status in {"concluído", "concluido"}


def agendamentos_pendentes_confirmacao_saida(codigo_porto, agora=None):
    """Lista navios vencidos que já podem ser perguntados novamente."""
    agora = agora or datetime.now()
    return (
        Agendamento.query.filter(
            Agendamento.codigo_porto == codigo_porto,
            Agendamento.ets.isnot(None),
            Agendamento.ets <= agora,
            db.or_(
                Agendamento.verificar_saida_adiada_ate.is_(None),
                Agendamento.verificar_saida_adiada_ate <= agora,
            ),
            db.func.lower(Agendamento.status).notin_(["concluído", "concluido"]),
        )
        .order_by(Agendamento.ets.asc())
        .all()
    )


# Transformacao de agendamento do banco para dados exibidos na tela
def serializar_agendamento(agendamento):
    labels = {
        "Atracacao": "Atracação",
        "Saida": "Saída",
        "Inspecao": "Inspeção",
        "Manutencao": "Manutenção",
    }
    tipo_label = labels.get(agendamento.tipo_agendamento, agendamento.tipo_agendamento)
    return {
        "id": agendamento.id,
        "texto": tipo_label,
        "tipo": tipo_para_classe(agendamento.tipo_agendamento),
        "tipo_agendamento": tipo_label,
        "tipo_fluxo": agendamento.tipo_fluxo,
        "tipo_carga": agendamento.tipo_carga,
        "status": agendamento.status,
        "previsao": agendamento.previsao.strftime("%d/%m/%Y às %H:%M"),
        "bandeira": agendamento.bandeira,
        "peso_bruto": agendamento.peso_bruto,
        "observacao": agendamento.observacao,
        "dimensoes": agendamento.dimensoes,
    }


# Transformacao atualizada de agendamento para calendario por tipo de carga
def serializar_agendamento(agendamento):
    tipo_classe = carga_para_classe(agendamento.tipo_carga)
    legenda = CALENDARIO_CORES.get(tipo_classe, CALENDARIO_CORES["geral"])
    eta = agendamento.eta or agendamento.previsao
    nome_embarcacao = agendamento.nome_embarcacao or "Embarcação não informada"
    embarcacao_informada = bool(agendamento.nome_embarcacao)
    return {
        "id": agendamento.id,
        "texto": nome_embarcacao,
        "embarcacao_informada": embarcacao_informada,
        "tipo": tipo_classe,
        "cor_label": legenda["label"],
        "navio_ideal": legenda["navio"],
        "tipo_agendamento": agendamento.tipo_agendamento,
        "tipo_fluxo": agendamento.tipo_fluxo,
        "tipo_carga": agendamento.tipo_carga,
        "status": agendamento.status,
        "previsao": formatar_data_hora(agendamento.previsao),
        "eta": formatar_data_hora(eta),
        "etb": formatar_data_hora(agendamento.etb),
        "ets": formatar_data_hora(agendamento.ets),
        "bandeira": agendamento.bandeira,
        "peso_bruto": agendamento.peso_bruto,
        "observacao": agendamento.observacao,
        "dimensoes": agendamento.dimensoes,
        "tipo_navio": agendamento.tipo_navio,
        "fcl_lcl": agendamento.fcl_lcl,
        "temperatura": agendamento.temperatura,
        "capacidade_navio": agendamento.capacidade_navio,
        "plano_carga": agendamento.plano_carga,
        "nome_embarcacao": agendamento.nome_embarcacao,
        "numero_viagem": agendamento.numero_viagem,
        "armador": agendamento.armador,
        "calado": agendamento.calado,
        "gate_cutoff": formatar_data_hora(agendamento.gate_cutoff),
        "cargo_doc_cutoff": formatar_data_hora(agendamento.cargo_doc_cutoff),
        "portos_escala": agendamento.portos_escala,
    }


# Carregamento dos eventos do calendario por mes e ano
def carregar_eventos_calendario(ano, mes):
    inicio = datetime(ano, mes, 1)
    fim = datetime(ano, mes, monthrange(ano, mes)[1], 23, 59, 59)
    eventos = {}
    agendamentos = (
        Agendamento.query.filter(
            Agendamento.codigo_porto == session.get("porto"),
            Agendamento.previsao >= inicio,
            Agendamento.previsao <= fim,
        )
        .order_by(Agendamento.previsao.asc())
        .all()
    )
    for agendamento in agendamentos:
        if passagem_pelo_porto_concluida(agendamento):
            continue
        eventos.setdefault(agendamento.previsao.day, []).append(serializar_agendamento(agendamento))
    return eventos


# Criacao de um novo agendamento operacional
def criar_agendamento(data):
    campos = [
        "tipo_agendamento",
        "tipo_carga",
        "data_previsao",
        "hora_previsao",
        "bandeira",
        "peso_bruto",
        "observacao",
        "dimensoes",
    ]
    valores = {campo: (data.get(campo) or "").strip() for campo in campos}
    if not all(valores.values()):
        return "Preencha todos os campos do agendamento."

    try:
        previsao = datetime.fromisoformat(f"{valores['data_previsao']}T{valores['hora_previsao']}")
    except ValueError:
        return "Data e horário da previsão inválidos."

    tipo_agendamento = valores["tipo_agendamento"]
    agendamento = Agendamento(
        codigo_porto=session.get("porto"),
        tipo_agendamento=tipo_agendamento,
        tipo_fluxo=fluxo_por_tipo_agendamento(tipo_agendamento),
        tipo_carga=valores["tipo_carga"],
        status=status_por_previsao(previsao),
        previsao=previsao,
        bandeira=valores["bandeira"],
        peso_bruto=valores["peso_bruto"],
        observacao=valores["observacao"],
        dimensoes=valores["dimensoes"],
        criado_por=session.get("usuario"),
    )
    db.session.add(agendamento)
    registrar_notificacao_calendario(agendamento)
    db.session.commit()
    return None


# Conversao de campos datetime-local do formulario
def parse_data_operacional(valor, campo):
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        raise ValueError(f"{campo} inválido.")


# Criacao atualizada de agendamento com dados logisticos completos
def criar_agendamento(data):
    campos_obrigatorios = [
        "tipo_carga",
        "tipo_navio",
        "fcl_lcl",
        "capacidade_navio",
        "plano_carga",
        "eta",
        "etb",
        "ets",
        "nome_embarcacao",
        "numero_viagem",
        "armador",
        "calado",
        "gate_cutoff",
        "cargo_doc_cutoff",
        "bandeira",
        "peso_bruto",
        "dimensoes",
        "observacao",
    ]
    valores = {campo: (data.get(campo) or "").strip() for campo in campos_obrigatorios}
    temperatura = (data.get("temperatura") or "").strip()
    portos_escala = (data.get("portos_escala") or "").strip()

    if not all(valores.values()):
        return "Preencha todos os campos obrigatórios do agendamento."
    if carga_para_classe(valores["tipo_carga"]) == "frigorifica" and not temperatura:
        return "Informe a temperatura exigida para carga frigorifica."

    try:
        eta = parse_data_operacional(valores["eta"], "ETA")
        etb = parse_data_operacional(valores["etb"], "ETB")
        ets = parse_data_operacional(valores["ets"], "ETS")
        gate_cutoff = parse_data_operacional(valores["gate_cutoff"], "Gate Cut-off")
        cargo_doc_cutoff = parse_data_operacional(valores["cargo_doc_cutoff"], "Cargo/Doc Cut-off")
    except ValueError as error:
        return str(error)

    if not eta <= etb <= ets:
        return "Use a ordem correta dos horários: ETA deve vir antes de ETB, e ETB antes de ETS."

    tipo_agendamento = "Operação de carga"
    agendamento = Agendamento(
        codigo_porto=session.get("porto"),
        tipo_agendamento=tipo_agendamento,
        tipo_fluxo="Operacional",
        tipo_carga=valores["tipo_carga"],
        status=status_por_previsao(eta),
        previsao=eta,
        bandeira=valores["bandeira"],
        peso_bruto=valores["peso_bruto"],
        observacao=valores["observacao"],
        dimensoes=valores["dimensoes"],
        tipo_navio=valores["tipo_navio"],
        fcl_lcl=valores["fcl_lcl"],
        temperatura=temperatura,
        capacidade_navio=valores["capacidade_navio"],
        plano_carga=valores["plano_carga"],
        eta=eta,
        etb=etb,
        ets=ets,
        nome_embarcacao=valores["nome_embarcacao"],
        numero_viagem=valores["numero_viagem"],
        armador=valores["armador"],
        calado=valores["calado"],
        gate_cutoff=gate_cutoff,
        cargo_doc_cutoff=cargo_doc_cutoff,
        portos_escala=portos_escala,
        criado_por=session.get("usuario"),
    )
    db.session.add(agendamento)
    registrar_notificacao_calendario(agendamento)
    db.session.commit()
    return None


# Calculo do risco de seca com base na chuva prevista
def calcular_risco_seca(total_chuva_7d):
    if total_chuva_7d <= 5:
        return {
            "nivel": "Alto",
            "classe": "alta",
            "mensagem": "Pouca chuva prevista. Risco maior de restrição operacional por seca.",
        }
    if total_chuva_7d <= 15:
        return {
            "nivel": "Moderado",
            "classe": "moderada",
            "mensagem": "Chuva prevista abaixo do ideal. Acompanhe calado e janelas de atracação.",
        }
    return {
        "nivel": "Baixo",
        "classe": "baixa",
        "mensagem": "Previsão de chuva reduz o risco imediato de seca operacional.",
    }


# Calculo do alerta de chuva intensa nas proximas 24 horas
def calcular_alerta_chuva(chuva_24h):
    if chuva_24h >= 50:
        return {
            "nivel": "Crítico",
            "classe": "critico",
            "mensagem": "Chuvas muito fortes previstas para as próximas 24 horas.",
        }
    if chuva_24h >= 30:
        return {
            "nivel": "Risco",
            "classe": "risco",
            "mensagem": "Chuvas fortes previstas para as próximas 24 horas.",
        }
    if chuva_24h >= 10:
        return {
            "nivel": "Atenção",
            "classe": "atencao",
            "mensagem": "Chuva moderada prevista. Acompanhe o pátio, as amarrações e as cargas sensíveis.",
        }
    return {
        "nivel": "Normal",
        "classe": "normal",
        "mensagem": "Sem indício de chuva intensa nas próximas 24 horas.",
    }


def classe_alerta_geral(chuva_classe, seca_classe):
    prioridades = {
        "critico": 4,
        "alta": 4,
        "risco": 3,
        "moderada": 2,
        "atencao": 2,
        "baixa": 1,
        "normal": 1,
        "indisponivel": 2,
    }
    classe = max([chuva_classe, seca_classe], key=lambda item: prioridades.get(item, 0))
    return {
        "critico": "Crítico",
        "alta": "Crítico",
        "risco": "Risco",
        "moderada": "Atenção",
        "atencao": "Atenção",
        "baixa": "Normal",
        "normal": "Normal",
        "indisponivel": "Atenção",
    }.get(classe, "Atenção")


# Consulta externa da previsao de chuva para o porto
def consultar_risco_seca(porto):
    params = urlencode(
        {
            "latitude": porto["lat"],
            "longitude": porto["lon"],
            "current": "temperature_2m,relative_humidity_2m,precipitation,rain,wind_speed_10m,wind_gusts_10m",
            "hourly": "precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m",
            "daily": "precipitation_sum",
            "forecast_days": 7,
            "timezone": "America/Manaus",
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    with urlopen(url, timeout=8) as response:
        data = response.read().decode("utf-8")

    import json

    payload = json.loads(data)
    chuva_diaria = payload.get("daily", {}).get("precipitation_sum", [])
    hourly = payload.get("hourly", {})
    current = payload.get("current", {})
    times = hourly.get("time", [])
    current_time = current.get("time")
    first_forecast_hour = next(
        (index for index, hour in enumerate(times) if current_time and hour >= current_time),
        0,
    )
    forecast_window = [
        {
            "hora": times[index],
            "chuva_mm": hourly.get("precipitation", [])[index],
            "probabilidade": hourly.get("precipitation_probability", [])[index],
            "vento_kmh": hourly.get("wind_speed_10m", [])[index],
            "rajada_kmh": hourly.get("wind_gusts_10m", [])[index],
        }
        for index in range(first_forecast_hour, min(first_forecast_hour + 24, len(times)))
    ]
    chuva_horaria = [hora["chuva_mm"] for hora in forecast_window]
    chuva_24h = round(sum(value for value in chuva_horaria if value is not None), 1)
    total_chuva = round(sum(chuva_diaria), 1)
    risco = calcular_risco_seca(total_chuva)
    chuva = calcular_alerta_chuva(chuva_24h)
    alertas_ativos = sum(
        [
            risco["classe"] in ["moderada", "alta"],
            chuva["classe"] in ["risco", "critico"],
        ]
    )
    return {
        "porto": porto["nome"],
        "codigo": porto["codigo"],
        "hidrovia": porto["hidrovia"],
        "chuva_7d_mm": total_chuva,
        "chuva_24h_mm": chuva_24h,
        "clima_atual": {
            "hora": current.get("time"),
            "temperatura_c": current.get("temperature_2m"),
            "umidade_pct": current.get("relative_humidity_2m"),
            "chuva_mm": current.get("precipitation"),
            "vento_kmh": current.get("wind_speed_10m"),
            "rajada_kmh": current.get("wind_gusts_10m"),
        },
        "previsao_horaria": forecast_window,
        "nivel": risco["nivel"],
        "classe": risco["classe"],
        "mensagem": risco["mensagem"],
        "chuva_nivel": chuva["nivel"],
        "chuva_classe": chuva["classe"],
        "chuva_mensagem": chuva["mensagem"],
        "alertas_ativos": alertas_ativos,
        "alerta_geral": classe_alerta_geral(chuva["classe"], risco["classe"]),
        "nivel_rio": {
            "disponivel": False,
            "valor": "Não disponível",
            "tendencia": "Não disponível",
            "variacao": "",
            "mensagem": "Acompanhe as medições das estações da bacia do Amazonas no portal oficial SGB/SACE.",
            "fonte": "SGB/SACE · Bacia do Amazonas",
        },
        "fonte": "Open-Meteo Forecast API",
    }


# Injecao dos itens de layout em todas as paginas
@app.context_processor
def inject_layout():
    user = usuario_logado() if session.get("logado") else None
    nav_items = list(NAV_ITEMS)
    if user and user.status_aprovacao == "aprovada" and usuario_e_fiscalizacao(user):
        nav_items.append(NAV_GERENCIAMENTO)
    nav_items.append(("Perfil", "perfil"))
    return {
        "nav_items": nav_items,
        "cargo_exibicao": cargo_exibicao,
        "formatar_data": formatar_data,
        "formatar_data_input": formatar_data_input,
    }


@app.errorhandler(403)
def forbidden_page(error):
    return render_template(
        "error.html",
        status_code=403,
        title="Acesso não permitido",
        message="Sua conta não tem permissão para acessar esta página. Volte ao painel ou entre com outra conta.",
    ), 403


@app.errorhandler(400)
def invalid_request_page(error):
    return render_template(
        "error.html",
        status_code=400,
        title="Confira os dados enviados",
        message="Não foi possível entender esta solicitação. Volte e revise os campos antes de tentar novamente.",
    ), 400


@app.errorhandler(404)
def not_found_page(error):
    return render_template(
        "error.html",
        status_code=404,
        title="Página não encontrada",
        message="O endereço pode ter mudado ou não existe. Volte ao painel para continuar.",
    ), 404


@app.errorhandler(500)
def internal_error_page(error):
    db.session.rollback()
    app.logger.error("Erro interno ao processar a solicitação: %s", error)
    return render_template(
        "error.html",
        status_code=500,
        title="Não foi possível concluir",
        message="O Fluir encontrou um erro inesperado. Tente novamente ou volte ao painel.",
    ), 500


# Rota inicial que redireciona conforme login
@app.route("/")
def index():
    if session.get("logado"):
        return redirect(url_for("home"))
    return redirect(url_for("login"))


# API simples para verificar se o backend esta rodando
@app.route("/api/status")
def api_status():
    return {"msg": "API rodando"}


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


# API de risco de seca usado pela Home
@app.route("/api/seca")
def api_seca():
    try:
        return jsonify(consultar_risco_seca(NOSSO_PORTO))
    except (URLError, TimeoutError, ValueError, OSError):
        return jsonify(
            {
                "porto": NOSSO_PORTO["nome"],
                "codigo": NOSSO_PORTO["codigo"],
                "hidrovia": NOSSO_PORTO["hidrovia"],
                "nivel": "Indisponível",
                "classe": "indisponivel",
                "chuva_7d_mm": None,
                "mensagem": "Não foi possível consultar a previsão real de chuva agora.",
                "fonte": "Open-Meteo Forecast API",
            }
        ), 503


# Login de usuario com validacao de porto e sessao
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    data = get_payload()
    login_identifier = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()
    codigo_porto = normalizar_codigo_porto(data.get("codigo_porto"))
    user = User.query.filter(db.func.lower(User.email) == login_identifier).first()
    porto = Porto.query.filter_by(codigo_portuario=codigo_porto).first()

    if not all([login_identifier, password, codigo_porto]):
        payload, status = {"error": "Preencha todos os campos"}, 400
    elif not user:
        payload, status = {"error": "E-mail não cadastrado"}, 404
    elif not porto or user.codigo_porto != porto.codigo_portuario:
        payload, status = {"error": "Código Portuário incorreto"}, 401
    elif not conferir_senha(user.password, password):
        payload, status = {"error": "Senha incorreta"}, 401
    elif user.status_aprovacao in {"pendente", "revisao", "rejeitada"}:
        if wants_json():
            return jsonify({"status": user.status_aprovacao, "redirect": url_for("aguardando_aprovacao")}), 202
        session.clear()
        session["cadastro_pendente_id"] = user.id
        return redirect(url_for("aguardando_aprovacao"))
    elif user.deve_trocar_senha:
        session.clear()
        session["senha_troca_pendente_id"] = user.id
        if wants_json():
            return jsonify({"status": "troca_senha", "redirect": url_for("trocar_senha_obrigatoria")}), 202
        return redirect(url_for("trocar_senha_obrigatoria"))
    else:
        if not senha_esta_protegida(user.password):
            user.password = generate_password_hash(password)
        user.last_access_at = datetime.now()
        db.session.commit()
        session.permanent = data.get("remember") in ["on", "true", True, "1", 1]
        session["logado"] = True
        session["usuario"] = user.username
        session["porto"] = porto.codigo_portuario
        payload, status = {"msg": "Login realizado com sucesso!"}, 200

    if wants_json():
        return jsonify(payload), status
    if status == 200:
        return redirect(url_for("home"))
    flash(payload["error"], "error")
    return render_template("login.html", form={**data, "email": login_identifier}), status


@app.route("/senha/troca-obrigatoria", methods=["GET", "POST"])
def trocar_senha_obrigatoria():
    usuario_id = session.get("senha_troca_pendente_id")
    usuario = db.session.get(User, usuario_id) if usuario_id else None
    if not usuario or usuario.status_aprovacao != "aprovada" or not usuario.deve_trocar_senha:
        session.pop("senha_troca_pendente_id", None)
        return redirect(url_for("login"))
    if request.method == "POST":
        senha_nova = (request.form.get("senha_nova") or "").strip()
        confirmacao = (request.form.get("confirmar_senha") or "").strip()
        erros = validar_senha(senha_nova)
        if erros:
            flash(erros[0], "error")
        elif senha_nova != confirmacao:
            flash("As senhas não correspondem.", "error")
        else:
            usuario.password = generate_password_hash(senha_nova)
            usuario.deve_trocar_senha = False
            usuario.last_access_at = datetime.now()
            db.session.commit()
            session.pop("senha_troca_pendente_id", None)
            session["logado"] = True
            session["usuario"] = usuario.username
            session["porto"] = usuario.codigo_porto
            session.permanent = True
            flash("Senha alterada. Acesso liberado.", "success")
            return redirect(url_for("home"))
    return render_template("troca_senha_obrigatoria.html", nome=usuario.nome_completo)


@app.route("/cadastro/aguardando")
def aguardando_aprovacao():
    user_id = session.get("cadastro_pendente_id")
    if not user_id:
        return redirect(url_for("login"))
    usuario = db.session.get(User, user_id)
    if not usuario:
        session.clear()
        return redirect(url_for("login"))
    acoes_rejeicao = HistoricoGerenciamento.query.filter(
        HistoricoGerenciamento.funcionario_id == usuario.id,
        HistoricoGerenciamento.acao.in_(["rejeicao_confirmada", "rejeicao_solicitada", "rejeitar"]),
    ).order_by(HistoricoGerenciamento.criado_em.desc()).all()
    motivo_rejeicao = ""
    for acao_rejeicao in acoes_rejeicao:
        try:
            motivo_rejeicao = json.loads(acao_rejeicao.detalhes or "{}").get("justificativa", "")
        except (TypeError, json.JSONDecodeError):
            motivo_rejeicao = ""
        if motivo_rejeicao:
            break
    return render_template("aguardando_aprovacao.html", nome=usuario.nome_completo, status=usuario.status_aprovacao, motivo_rejeicao=motivo_rejeicao)


@app.route("/cadastro/contestar", methods=["POST"])
def contestar_rejeicao_cadastro():
    usuario_id = session.get("cadastro_pendente_id")
    usuario = db.session.get(User, usuario_id) if usuario_id else None
    if not usuario or usuario.status_aprovacao != "rejeitada":
        abort(403)
    justificativa = (request.form.get("justificativa") or "").strip()
    arquivos = request.files.getlist("imagens")
    arquivos = [arquivo for arquivo in arquivos if arquivo and arquivo.filename]
    if len(justificativa) > 2000:
        flash("A justificativa deve ter até 2.000 caracteres.", "error")
        return redirect(url_for("aguardando_aprovacao"))
    if len(arquivos) > 3:
        flash("Você pode anexar no máximo 3 imagens.", "error")
        return redirect(url_for("aguardando_aprovacao"))
    extensoes = [validar_arquivo_avatar(arquivo) for arquivo in arquivos]
    if any(not extensao for extensao in extensoes):
        flash("As imagens devem ser JPG, PNG ou WebP válidas e ter até 5 MB.", "error")
        return redirect(url_for("aguardando_aprovacao"))
    imagens_salvas = []
    APPEAL_DIR.mkdir(parents=True, exist_ok=True)
    for arquivo, extensao in zip(arquivos, extensoes):
        nome_arquivo = f"{uuid4().hex}{extensao}"
        arquivo.save(APPEAL_DIR / nome_arquivo)
        imagens_salvas.append(f"uploads/contestacoes/{nome_arquivo}")
    status_anterior = usuario.status_aprovacao
    usuario.status_aprovacao = "revisao"
    historico = HistoricoGerenciamento(
        ator_id=usuario.id,
        ator_nome=usuario.nome_completo,
        funcionario_id=usuario.id,
        funcionario_nome=usuario.nome_completo,
        codigo_porto=usuario.codigo_porto,
        acao="contestacao",
        detalhes=json.dumps({"justificativa": justificativa, "imagens": imagens_salvas, "status_anterior": status_anterior}, ensure_ascii=False),
        criado_em=datetime.now(),
    )
    db.session.add(historico)
    db.session.commit()
    flash("Sua contestação foi enviada para análise da Fiscalização e Controle.", "success")
    return redirect(url_for("aguardando_aprovacao"))


@app.route("/funcionarios/contestacao/<filename>")
def visualizar_imagem_contestacao(filename):
    blocked = login_required()
    if blocked:
        return blocked
    obter_autorizacao_gerenciamento(representante=True)
    filename = secure_filename(filename)
    if not filename:
        abort(404)
    return send_from_directory(APPEAL_DIR, filename)


@app.route("/api/cadastro/status")
def status_cadastro_pendente():
    user_id = session.get("cadastro_pendente_id")
    usuario = db.session.get(User, user_id) if user_id else None
    if not usuario:
        return jsonify({"status": "indisponivel"}), 401
    if usuario.status_aprovacao == "aprovada":
        session.pop("cadastro_pendente_id", None)
        session["logado"] = True
        session["usuario"] = usuario.username
        session["porto"] = usuario.codigo_porto
        return jsonify({"status": "aprovada", "redirect": url_for("home")})
    return jsonify({"status": usuario.status_aprovacao})


# Logout e limpeza da sessao
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    response = redirect(url_for("login"))
    response.set_cookie("fluir-theme", "dark", max_age=31536000, path="/", samesite="Lax")
    return response


def apagar_foto_anterior(caminho):
    if not caminho:
        return
    arquivo = AVATAR_DIR / Path(caminho).name
    try:
        arquivo.resolve().relative_to(AVATAR_DIR.resolve())
    except ValueError:
        return
    arquivo.unlink(missing_ok=True)


def validar_arquivo_avatar(arquivo):
    nome_seguro = secure_filename(arquivo.filename or "")
    extensao = Path(nome_seguro).suffix.lower()
    if extensao not in ALLOWED_AVATAR_EXTENSIONS:
        return None
    arquivo.stream.seek(0, 2)
    tamanho = arquivo.stream.tell()
    arquivo.stream.seek(0)
    if not tamanho or tamanho > MAX_IMAGE_FILE_SIZE:
        return None
    assinatura = arquivo.stream.read(12)
    arquivo.stream.seek(0)
    imagem_valida = (
        (extensao in {".jpg", ".jpeg"} and assinatura.startswith(b"\xff\xd8\xff"))
        or (extensao == ".png" and assinatura.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extensao == ".webp" and assinatura[:4] == b"RIFF" and assinatura[8:12] == b"WEBP")
    )
    return extensao if imagem_valida else None


@app.route("/perfil", methods=["GET", "POST"])
def perfil():
    blocked = login_required()
    if blocked:
        return blocked
    usuario = usuario_logado()
    if not usuario:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        if float(session.get("perfil_edicao_expira", 0)) <= time.time():
            flash("Confirme sua senha antes de editar os dados da conta.", "error")
            return redirect(url_for("perfil"))
        nome = (request.form.get("nome_completo") or "").strip()
        email = email_valido_e_disponivel(request.form.get("email"), usuario.id)
        nascimento = formatar_data((request.form.get("data_nascimento") or "").strip())
        if not nome:
            flash("Informe seu nome completo.", "error")
        elif not email:
            flash("Informe um e-mail válido e ainda não utilizado.", "error")
        else:
            usuario.nome_completo = nome
            usuario.email = email
            usuario.data_nascimento = nascimento
            db.session.commit()
            session.pop("perfil_edicao_expira", None)
            flash("Seus dados foram atualizados.", "success")
            return redirect(url_for("perfil"))

    porto = Porto.query.filter_by(codigo_portuario=usuario.codigo_porto).first()
    avatar_file = BASE_DIR / "static" / usuario.avatar_path if usuario.avatar_path else None
    avatar_version = avatar_file.stat().st_mtime_ns if avatar_file and avatar_file.is_file() else ""
    return render_template(
        "perfil.html",
        usuario=usuario,
        tipo_conta="Representante do Setor" if usuario.poder_maior else "Funcionário",
        setor_exibicao=cargo_exibicao(usuario.cargo),
        porto=porto,
        editando_dados=float(session.get("perfil_edicao_expira", 0)) > time.time(),
        avatar_url=url_for("static", filename=usuario.avatar_path, v=avatar_version) if avatar_file and avatar_file.is_file() else None,
    )


@app.route("/perfil/autorizar-edicao", methods=["POST"])
def autorizar_edicao_perfil():
    blocked = login_required()
    if blocked:
        return blocked
    usuario = usuario_logado()
    if not conferir_senha(usuario.password, request.form.get("senha_atual") or ""):
        flash("A senha atual não confere. Seus dados continuam protegidos.", "error")
        return redirect(url_for("perfil"))
    session["perfil_edicao_expira"] = time.time() + 300
    return redirect(url_for("perfil", editar=1))


@app.route("/perfil/cancelar-edicao", methods=["POST"])
def cancelar_edicao_perfil():
    blocked = login_required()
    if blocked:
        return blocked
    session.pop("perfil_edicao_expira", None)
    return redirect(url_for("perfil"))


@app.route("/perfil/foto", methods=["POST"])
def atualizar_foto_perfil():
    blocked = login_required()
    if blocked:
        return blocked
    usuario = usuario_logado()
    arquivo = request.files.get("foto")
    extensao = validar_arquivo_avatar(arquivo) if arquivo else None
    if not extensao:
        flash("Escolha uma imagem JPG, PNG ou WebP de até 5 MB.", "error")
        return redirect(url_for("perfil"))

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    nome_arquivo = f"{uuid4().hex}{extensao}"
    caminho = AVATAR_DIR / nome_arquivo
    arquivo.save(caminho)
    caminho_anterior = usuario.avatar_path
    usuario.avatar_path = f"uploads/avatars/{nome_arquivo}"
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        caminho.unlink(missing_ok=True)
        flash("Não foi possível salvar sua foto agora.", "error")
        return redirect(url_for("perfil"))
    apagar_foto_anterior(caminho_anterior)
    flash("Foto de perfil atualizada.", "success")
    return redirect(url_for("perfil"))


@app.route("/perfil/foto/remover", methods=["POST"])
def remover_foto_perfil():
    blocked = login_required()
    if blocked:
        return blocked
    usuario = usuario_logado()
    caminho_anterior = usuario.avatar_path
    usuario.avatar_path = None
    db.session.commit()
    apagar_foto_anterior(caminho_anterior)
    flash("Foto de perfil removida.", "success")
    return redirect(url_for("perfil"))


@app.route("/perfil/senha", methods=["POST"])
def alterar_senha_perfil():
    blocked = login_required()
    if blocked:
        return blocked
    usuario = usuario_logado()
    atual = request.form.get("senha_atual") or ""
    nova = request.form.get("senha_nova") or ""
    confirmacao = request.form.get("confirmar_senha") or ""
    if not conferir_senha(usuario.password, atual):
        flash("A senha atual não confere.", "error")
    elif nova != confirmacao:
        flash("A nova senha e a confirmação estão diferentes.", "error")
    elif nova == atual:
        flash("Escolha uma senha diferente da atual.", "error")
    elif validar_senha(nova):
        flash(validar_senha(nova)[0], "error")
    else:
        usuario.password = generate_password_hash(nova)
        db.session.commit()
        flash("Senha alterada com sucesso.", "success")
    return redirect(url_for("perfil"))


# Tela de cadastro de funcionario ou porto
@app.route("/cadastro", methods=["GET"])
def cadastro():
    tipo = request.args.get("tipo", "Funcionario")
    if tipo not in ["Funcionario", "Porto"]:
        tipo = "Funcionario"
    return render_template("cadastro.html", tipo=tipo, cargos=CARGOS_FUNCIONARIO)


# Rota de envio do cadastro de funcionario
@app.route("/signup_funcionario", methods=["POST"])
def signup_funcionario():
    data = get_payload()
    payload, status = cadastro_funcionario_payload(data)

    if wants_json():
        return jsonify(payload), status
    if status == 201:
        flash(payload["msg"], "success")
        return redirect(url_for("login"))
    return render_template(
        "cadastro.html",
        tipo="Funcionario",
        form=data,
        cargos=CARGOS_FUNCIONARIO,
        senha_avisos=avisos_senha(
            data.get("password"),
            data.get("email"),
            data.get("nome_completo"),
            data.get("data_nascimento"),
        ),
        error=payload["error"],
    ), status


# Rota de envio do cadastro de porto
@app.route("/signup_porto", methods=["POST"])
def signup_porto():
    data = get_payload()
    payload, status = cadastro_porto_payload(data)

    if wants_json():
        return jsonify(payload), status
    if status == 201:
        flash(payload["msg"], "success")
        return redirect(url_for("login"))
    return render_template(
        "cadastro.html",
        tipo="Porto",
        form=data,
        cargos=CARGOS_FUNCIONARIO,
        error=payload["error"],
    ), status


# Home com resumo operacional do porto
@app.route("/home")
def home():
    blocked = login_required()
    if blocked:
        return blocked
    return render_template(
        "home.html",
        nosso_porto=NOSSO_PORTO,
        fluxos=FLUXOS_PORTUARIOS,
        total_saidas=sum(1 for fluxo in FLUXOS_PORTUARIOS if fluxo["tipo"] == "saida"),
        total_entradas=sum(1 for fluxo in FLUXOS_PORTUARIOS if fluxo["tipo"] == "entrada"),
        total_essenciais=sum(1 for fluxo in FLUXOS_PORTUARIOS if fluxo["prioridade"] == "Essencial"),
        notificacoes=carregar_notificacoes_home(),
    )


# Compatibilidade para redirecionar dashboard para Home
@app.route("/dashboard")
def dashboard_redirect():
    return redirect(url_for("home"))


@app.route("/funcionarios", methods=["GET"])
def gerenciar_funcionarios():
    blocked = login_required()
    if blocked:
        return blocked
    ator = obter_autorizacao_gerenciamento()
    representante = bool(ator.poder_maior)
    fiscalizacao = usuario_e_fiscalizacao(ator)
    termo = (request.args.get("q") or "").strip().casefold()
    filtro_cargo = (request.args.get("cargo") or "").strip()
    if filtro_cargo not in CARGOS_FUNCIONARIO:
        filtro_cargo = ""
    funcionarios = User.query.filter_by(codigo_porto=ator.codigo_porto).order_by(User.cargo, User.nome_completo).all()

    def combina(item):
        campos = [item.nome_completo, item.username, item.email or "", item.codigo_porto, item.cargo or ""]
        return (not filtro_cargo or item.cargo == filtro_cargo) and (not termo or remover_acentos(termo).casefold() in remover_acentos(" ".join(campos)).casefold())

    if representante:
        visiveis = [item for item in funcionarios if combina(item)]
    else:
        visiveis = [
            item for item in funcionarios
            if (
                (item.status_aprovacao == "pendente" and not usuario_e_fiscalizacao(item))
                or (item.status_aprovacao == "aprovada" and usuario_e_fiscalizacao(item))
            ) and combina(item)
        ]

    for item in visiveis:
        item.justificativa_rejeicao = ""
        item.justificativa_contestacao = ""
        item.imagens_contestacao = []
        contestacao_encontrada = False
        if item.status_aprovacao in {"revisao", "rejeitada"}:
            acoes = HistoricoGerenciamento.query.filter(
                HistoricoGerenciamento.funcionario_id == item.id,
                HistoricoGerenciamento.acao.in_(["contestacao", "rejeicao_solicitada", "rejeicao_confirmada", "rejeitar"]),
            ).order_by(HistoricoGerenciamento.criado_em.desc()).all()
            for acao in acoes:
                try:
                    detalhes = json.loads(acao.detalhes or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if acao.acao == "contestacao" and not contestacao_encontrada:
                    item.justificativa_contestacao = detalhes.get("justificativa", "")
                    item.imagens_contestacao = detalhes.get("imagens", [])
                    contestacao_encontrada = True
                elif acao.acao in {"rejeicao_solicitada", "rejeicao_confirmada", "rejeitar"} and not item.justificativa_rejeicao:
                    item.justificativa_rejeicao = detalhes.get("justificativa", "")

    grupos_por_status = {}
    for status in ("pendente", "revisao", "aprovada", "rejeitada"):
        por_cargo = {}
        ordenados = sorted(
            (usuario for usuario in visiveis if usuario.status_aprovacao == status),
            key=lambda usuario: (remover_acentos(cargo_exibicao(usuario.cargo)).casefold(), remover_acentos(usuario.nome_completo).casefold()),
        )
        for item in ordenados:
            por_cargo.setdefault(cargo_exibicao(item.cargo), []).append(item)
        grupos_por_status[status] = por_cargo

    historico = []
    if representante:
        historico = HistoricoGerenciamento.query.filter_by(codigo_porto=ator.codigo_porto).order_by(HistoricoGerenciamento.criado_em.desc()).limit(100).all()
        for acao in historico:
            try:
                detalhes = json.loads(acao.detalhes or "{}")
            except (TypeError, json.JSONDecodeError):
                detalhes = {}
            if acao.acao == "edicao":
                antes, depois = detalhes.get("antes", {}), detalhes.get("depois", {})
                alteracoes = [
                    f"{rotulo}: {antes.get(chave, '—')} → {depois.get(chave, '—')}"
                    for chave, rotulo in (("identificacao_interna", "Identificação interna"), ("nome_completo", "Nome"), ("email", "E-mail"), ("data_nascimento", "Nascimento"), ("cargo", "Cargo"), ("codigo_porto", "Porto"), ("representante", "Representante"), ("senha_redefinida", "Senha redefinida"))
                    if chave in depois and antes.get(chave) != depois.get(chave)
                ]
                acao.detalhe_exibicao = "; ".join(alteracoes)
            elif acao.acao in {"aprovar", "rejeitar", "rejeicao_solicitada", "rejeicao_confirmada"}:
                acao.detalhe_exibicao = f"Cargo: {cargo_exibicao(detalhes.get('cargo'))}"
            elif acao.acao == "exclusao":
                acao.detalhe_exibicao = f"Usuário: {detalhes.get('username', '—')} · Porto: {detalhes.get('codigo_porto', '—')}"
            else:
                acao.detalhe_exibicao = ""
    aprovados = [usuario for usuario in visiveis if usuario.status_aprovacao == "aprovada"]
    aprovacao_por_id = {}
    if representante and aprovados:
        ids = [usuario.id for usuario in aprovados]
        acoes = HistoricoGerenciamento.query.filter(HistoricoGerenciamento.funcionario_id.in_(ids), HistoricoGerenciamento.acao == "aprovar").order_by(HistoricoGerenciamento.criado_em.desc()).all()
        for acao in acoes:
            aprovacao_por_id.setdefault(acao.funcionario_id, acao)
    for usuario in aprovados:
        acao = aprovacao_por_id.get(usuario.id)
        usuario.aprovado_por = acao.ator_nome if acao else "Cadastro anterior ao histórico de aprovação"
        usuario.aprovado_em = acao.criado_em.strftime("%d/%m/%Y %H:%M") if acao else "Não disponível"
    return render_template("gerenciar_funcionarios.html", representante=representante, fiscalizacao=fiscalizacao, termo=termo, filtro_cargo=filtro_cargo, grupos=grupos_por_status, total=len(visiveis), historico=historico, cargos_funcionario=CARGOS_FUNCIONARIO)


@app.route("/funcionarios/<funcionario_id>/decisao", methods=["POST"])
def decidir_cadastro_funcionario(funcionario_id):
    blocked = login_required()
    if blocked:
        return blocked
    ator = obter_autorizacao_gerenciamento()
    funcionario = User.query.filter_by(id=funcionario_id, codigo_porto=ator.codigo_porto).first_or_404()
    if funcionario.id == ator.id:
        abort(403)
    if funcionario.status_aprovacao not in {"pendente", "revisao"}:
        flash("Essa solicitação já foi analisada.", "error")
        return redirect(url_for("gerenciar_funcionarios"))
    if funcionario.status_aprovacao == "revisao" and not ator.poder_maior:
        abort(403)
    if usuario_e_fiscalizacao(funcionario) and not ator.poder_maior:
        abort(403)
    decisao = request.form.get("decisao")
    if decisao not in {"aprovar", "rejeitar"}:
        abort(400)
    justificativa = (request.form.get("justificativa") or "").strip()
    em_revisao = funcionario.status_aprovacao == "revisao"
    if decisao == "rejeitar" and not ator.poder_maior and not em_revisao and not justificativa:
        flash("Escreva a justificativa da rejeição.", "error")
        return redirect(url_for("gerenciar_funcionarios"))
    if decisao == "rejeitar" and not ator.poder_maior and not em_revisao and len(justificativa) > 1000:
        flash("A justificativa deve ter até 1.000 caracteres.", "error")
        return redirect(url_for("gerenciar_funcionarios"))
    funcionario.status_aprovacao = "aprovada" if decisao == "aprovar" else ("revisao" if not ator.poder_maior and not em_revisao else "rejeitada")
    acao = "rejeicao_solicitada" if decisao == "rejeitar" and funcionario.status_aprovacao == "revisao" else ("rejeicao_confirmada" if decisao == "rejeitar" and em_revisao else decisao)
    detalhes = {"cargo": funcionario.cargo, "email": funcionario.email}
    if justificativa:
        detalhes["justificativa"] = justificativa
    registrar_acao_funcionario(ator, funcionario, acao, json.dumps(detalhes, ensure_ascii=False))
    db.session.commit()
    mensagem = "Cadastro aprovado. O funcionário já pode acessar o Fluir." if decisao == "aprovar" else ("Rejeição encaminhada para confirmação de um representante." if funcionario.status_aprovacao == "revisao" else "Cadastro rejeitado. O acesso permanece bloqueado.")
    flash(mensagem, "success")
    return redirect(url_for("gerenciar_funcionarios"))


@app.route("/funcionarios/<funcionario_id>/promover-representante", methods=["POST"])
def promover_representante_funcionario(funcionario_id):
    blocked = login_required()
    if blocked:
        return blocked
    ator = obter_autorizacao_gerenciamento()
    funcionario = User.query.filter_by(id=funcionario_id, codigo_porto=ator.codigo_porto).first_or_404()
    if (
        funcionario.id == ator.id
        or funcionario.status_aprovacao != "aprovada"
        or not usuario_e_fiscalizacao(funcionario)
        or funcionario.poder_maior
    ):
        abort(403)
    funcionario.poder_maior = True
    registrar_acao_funcionario(ator, funcionario, "promocao_representante", "Promovido por funcionário aprovado da Fiscalização e Controle")
    db.session.commit()
    flash(f"{funcionario.nome_completo} agora é representante da Fiscalização e Controle.", "success")
    return redirect(url_for("gerenciar_funcionarios"))


@app.route("/funcionarios/<funcionario_id>/editar", methods=["POST"])
def editar_funcionario(funcionario_id):
    blocked = login_required()
    if blocked:
        return blocked
    ator = obter_autorizacao_gerenciamento(representante=True)
    funcionario = User.query.filter_by(id=funcionario_id, codigo_porto=ator.codigo_porto).first_or_404()
    if funcionario.id == ator.id or funcionario.status_aprovacao != "aprovada":
        abort(403)
    nome = (request.form.get("nome_completo") or "").strip()
    identificacao_interna = (request.form.get("identificacao_interna") or "").strip().upper()
    email = email_valido_e_disponivel(request.form.get("email"), funcionario.id)
    cargo = (request.form.get("cargo") or "").strip()
    nascimento = formatar_data((request.form.get("data_nascimento") or "").strip())
    codigo_porto = funcionario.codigo_porto
    quer_representante = request.form.get("representante") == "sim"
    if not identificacao_interna or len(identificacao_interna) > 20 or not nome or not email or cargo not in CARGOS_FUNCIONARIO:
        flash("Confira identificação interna, nome, e-mail e cargo. O e-mail precisa ser válido e exclusivo.", "error")
        return redirect(url_for("gerenciar_funcionarios"))
    if User.query.filter(User.identificacao_interna == identificacao_interna, User.id != funcionario.id).first():
        flash("Esta identificação interna já está em uso.", "error")
        return redirect(url_for("gerenciar_funcionarios"))
    antes = {"identificacao_interna": funcionario.identificacao_interna, "nome_completo": funcionario.nome_completo, "email": funcionario.email, "data_nascimento": funcionario.data_nascimento, "cargo": funcionario.cargo, "codigo_porto": funcionario.codigo_porto, "representante": bool(funcionario.poder_maior)}
    novo_poder_maior = quer_representante
    permissao_removida = bool(funcionario.poder_maior and not novo_poder_maior)
    remove_representacao = bool(funcionario.poder_maior and (not novo_poder_maior or cargo != funcionario.cargo))
    if remove_representacao:
        outros = User.query.filter(User.codigo_porto == funcionario.codigo_porto, User.cargo == funcionario.cargo, User.poder_maior.is_(True), User.status_aprovacao == "aprovada", User.id != funcionario.id).all()
        if not outros:
            flash(f"Não é possível remover o último representante aprovado de {cargo_exibicao(funcionario.cargo)}.", "error")
            return redirect(url_for("gerenciar_funcionarios"))
    funcionario.identificacao_interna = identificacao_interna
    funcionario.nome_completo, funcionario.email, funcionario.cargo = nome, email, cargo
    funcionario.data_nascimento, funcionario.codigo_porto = nascimento, codigo_porto
    funcionario.poder_maior = novo_poder_maior
    depois = {"identificacao_interna": identificacao_interna, "nome_completo": nome, "email": email, "data_nascimento": nascimento, "cargo": cargo, "codigo_porto": codigo_porto, "representante": bool(funcionario.poder_maior)}
    registrar_acao_funcionario(ator, funcionario, "edicao", json.dumps({"antes": antes, "depois": depois}, ensure_ascii=False))
    db.session.commit()
    mensagem = "Dados do funcionário atualizados."
    if permissao_removida:
        mensagem += " A permissão de representante foi removida."
    flash(mensagem, "success")
    return redirect(url_for("gerenciar_funcionarios"))


@app.route("/funcionarios/<funcionario_id>/senha", methods=["POST"])
def redefinir_senha_funcionario(funcionario_id):
    blocked = login_required()
    if blocked:
        return blocked
    ator = obter_autorizacao_gerenciamento()
    funcionario = User.query.filter_by(id=funcionario_id, codigo_porto=ator.codigo_porto).first_or_404()
    if funcionario.id == ator.id or funcionario.status_aprovacao != "aprovada" or (not ator.poder_maior and not usuario_e_fiscalizacao(funcionario)):
        abort(403)
    funcionario.password = generate_password_hash("12345")
    funcionario.deve_trocar_senha = True
    registrar_acao_funcionario(ator, funcionario, "redefinicao_senha", "Senha temporária padrão definida; troca obrigatória no próximo acesso")
    db.session.commit()
    flash("Senha redefinida para 12345. O funcionário deverá criar uma nova senha no próximo acesso.", "success")
    return redirect(url_for("gerenciar_funcionarios"))


@app.route("/funcionarios/<funcionario_id>/excluir", methods=["POST"])
def excluir_funcionario(funcionario_id):
    blocked = login_required()
    if blocked:
        return blocked
    ator = obter_autorizacao_gerenciamento(representante=True)
    funcionario = User.query.filter_by(id=funcionario_id, codigo_porto=ator.codigo_porto).first_or_404()
    if not usuario_e_fiscalizacao(funcionario) or funcionario.id == ator.id or funcionario.status_aprovacao != "aprovada":
        abort(403)
    if request.form.get("confirmar") != "sim":
        abort(400)
    if funcionario.poder_maior:
        outros = User.query.filter(User.codigo_porto == ator.codigo_porto, User.poder_maior.is_(True), User.status_aprovacao == "aprovada", User.id != funcionario.id).all()
        if not any(usuario_e_fiscalizacao(item) for item in outros):
            flash("Não é possível excluir o último representante aprovado de Fiscalização e Controle.", "error")
            return redirect(url_for("gerenciar_funcionarios"))
    nome, foto = funcionario.nome_completo, funcionario.avatar_path
    registrar_acao_funcionario(ator, funcionario, "exclusao", json.dumps({"username": funcionario.username, "codigo_porto": funcionario.codigo_porto}, ensure_ascii=False))
    db.session.delete(funcionario)
    db.session.commit()
    apagar_foto_anterior(foto)
    flash(f"A conta de {nome} foi excluída. As mensagens e os registros históricos foram preservados.", "success")
    return redirect(url_for("gerenciar_funcionarios"))


# Acesso ao chat em tempo real pelo menu do Flask
def ambiente_servidor_chat():
    ambiente = os.environ.copy()
    sites = []
    runtime_site = Path(sys.executable).resolve().parent / "Lib" / "site-packages"
    for candidate in [runtime_site, *(Path(item) for item in sys.path if item), BASE_DIR / "venv" / "Lib" / "site-packages"]:
        if candidate.is_dir() and str(candidate) not in sites:
            sites.append(str(candidate))

    versao_python = f"cp{sys.version_info.major}{sys.version_info.minor}"
    site_compativel = next(
        (
            site
            for site in sites
            if any((Path(site) / "pydantic_core").glob(f"_pydantic_core.{versao_python}_*.pyd"))
        ),
        None,
    )
    if site_compativel:
        sites.remove(site_compativel)
        sites.insert(0, site_compativel)
    anteriores = ambiente.get("PYTHONPATH", "").split(os.pathsep)
    for item in anteriores:
        if item and item not in sites:
            sites.append(item)
    ambiente["PYTHONPATH"] = os.pathsep.join(sites)
    return ambiente


def garantir_servidor_chat():
    global _chat_server_process, CHAT_BASE_URL

    def versao_ativa():
        try:
            with urlopen(f"{CHAT_BASE_URL}/health", timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("status") == "ok":
                    return payload.get("build") or "legacy"
        except (URLError, TimeoutError, OSError, ValueError):
            pass
        return None

    build = versao_ativa()
    if build == CHAT_BUILD_ID:
        return True

    # Em desenvolvimento local, o Flask inicia o serviço do chat se ele ainda
    # não estiver rodando. Um endereço remoto configurado continua externo.
    if not CHAT_BASE_URL.startswith(("http://127.0.0.1:", "http://localhost:")):
        return False

    with _chat_start_lock:
        build = versao_ativa()
        if build == CHAT_BUILD_ID:
            return True

        # A versão anterior sempre respondia /health como saudável, mesmo
        # mantendo as regras de autorização antigas em memória. Quando ela
        # estiver presa à porta padrão, sobe a versão atual em uma porta livre.
        if build is not None:
            for port in range(8002, 8020):
                CHAT_BASE_URL = f"http://127.0.0.1:{port}"
                candidate_build = versao_ativa()
                if candidate_build == CHAT_BUILD_ID:
                    return True
                if candidate_build is not None:
                    continue
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                        probe.bind(("127.0.0.1", port))
                    break
                except OSError:
                    continue
            else:
                app.logger.error("Não há uma porta livre para iniciar o chat atualizado.")
                return False

        if _chat_server_process is None or _chat_server_process.poll() is not None:
            log_path = BASE_DIR / "instance" / "chat-server.log"
            log_path.parent.mkdir(exist_ok=True)
            try:
                with log_path.open("ab") as log_file:
                    _chat_server_process = subprocess.Popen(
                        [sys.executable, "-m", "uvicorn", "chat_fastapi:app", "--host", "127.0.0.1", "--port", str(urlsplit(CHAT_BASE_URL).port or 8001)],
                        cwd=BASE_DIR,
                        env=ambiente_servidor_chat(),
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        | getattr(subprocess, "DETACHED_PROCESS", 0),
                    )
            except OSError:
                app.logger.exception("Não foi possível iniciar o serviço local do chat.")
                return False

        for _ in range(25):
            if versao_ativa() == CHAT_BUILD_ID:
                return True
            if _chat_server_process.poll() is not None:
                break
            time.sleep(0.2)
    return versao_ativa() == CHAT_BUILD_ID


@app.route("/chat")
def chat_live():
    blocked = login_required()
    if blocked:
        return blocked
    usuario = usuario_logado()
    if not usuario:
        flash("Usuário não encontrado para acessar o chat.", "error")
        return redirect(url_for("home"))
    if not garantir_servidor_chat():
        flash("O chat não abriu desta vez. Volte ao painel e tente novamente; se continuar, avise a equipe responsável pelo Fluir.", "error")
        return redirect(url_for("home"))
    setor = usuario.cargo or "Setor nao informado"
    room = f"{session.get('porto')}-{setor_para_sala(setor)}"
    params = urlencode(
        {
            "user": session.get("usuario") or "Funcionario",
            "nome_completo": usuario.nome_completo,
            "setor_exibicao": "Representante do Setor" if usuario.poder_maior else setor,
            "porto": session.get("porto") or NOSSO_PORTO["codigo"],
            "setor": setor,
            "room": room,
            "token": gerar_token_chat(session.get("usuario"), session.get("porto"), setor),
        }
    )
    return redirect(f"{CHAT_BASE_URL}/chat?{params}")


@app.get("/api/chat/unread-total")
def chat_unread_total():
    blocked = login_required()
    if blocked:
        return jsonify({"count": 0}), 401
    usuario = usuario_logado()
    if not usuario:
        return jsonify({"count": 0}), 401

    user = session.get("usuario") or "Funcionario"
    porto = session.get("porto") or NOSSO_PORTO["codigo"]
    setor = usuario.cargo or "Setor nao informado"
    params = urlencode({
        "user": user,
        "porto": porto,
        "setor": setor,
        "token": gerar_token_chat(user, porto, setor),
    })
    try:
        with urlopen(f"{CHAT_BASE_URL}/unread?{params}", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("unread"), dict):
            return jsonify({"count": 0, "available": False}), 503
        count = sum(max(0, int(value)) for value in payload.get("unread", {}).values())
        return jsonify({"count": count})
    except (OSError, URLError, ValueError, TypeError):
        return jsonify({"count": 0, "available": False}), 503


# Calendario operacional mensal
@app.route("/calendario")
def calendario():
    blocked = login_required()
    if blocked:
        return blocked
    hoje = date.today()
    ano = request.args.get("ano", default=hoje.year, type=int)
    mes = request.args.get("mes", default=hoje.month, type=int)
    if not 1 <= mes <= 12:
        mes = hoje.month
    primeiro_dia_semana, dias_no_mes = monthrange(ano, mes)
    espacos_inicio = (primeiro_dia_semana + 1) % 7
    mes_anterior = 12 if mes == 1 else mes - 1
    ano_anterior = ano - 1 if mes == 1 else ano
    proximo_mes = 1 if mes == 12 else mes + 1
    proximo_ano = ano + 1 if mes == 12 else ano
    is_admin = usuario_e_administrador_portuario()
    return render_template(
        "calendario.html",
        ano=ano,
        mes=mes,
        mes_nome=list(MESES.keys())[mes - 1],
        dias_no_mes=dias_no_mes,
        espacos_inicio=espacos_inicio,
        hoje=hoje,
        mes_anterior=mes_anterior,
        ano_anterior=ano_anterior,
        proximo_mes=proximo_mes,
        proximo_ano=proximo_ano,
        eventos=carregar_eventos_calendario(ano, mes),
        pendencias_saida=(
            agendamentos_pendentes_confirmacao_saida(session.get("porto"))
            if is_admin else []
        ),
        legenda=CALENDARIO_CORES,
        is_admin=is_admin,
    )


# Historico anual de navios e cargas registrados para o porto atual
@app.route("/historico-navios")
def historico_navios():
    blocked = login_required()
    if blocked:
        return blocked

    registros = (
        Agendamento.query.filter_by(codigo_porto=session.get("porto"))
        .all()
    )
    registros.sort(key=lambda item: item.eta or item.previsao, reverse=True)

    historico = {}
    for registro in registros:
        # O histórico contém somente saídas confirmadas por um representante.
        if not passagem_pelo_porto_concluida(registro):
            continue
        data_registro = registro.concluido_em or registro.eta or registro.previsao
        ano = data_registro.year
        data_chave = data_registro.date()
        historico.setdefault(ano, {}).setdefault(data_chave, []).append(
            {
                "tipo_carga": registro.tipo_carga or "Carga não informada",
                "nome_embarcacao": registro.nome_embarcacao or "Navio não informado",
                "hora": data_registro.strftime("%H:%M"),
                "tipo_fluxo": registro.tipo_fluxo,
                "numero_viagem": registro.numero_viagem,
                "armador": registro.armador,
            }
        )

    return render_template("historico_navios.html", historico=historico)


@app.route("/agendamentos/<agendamento_id>/confirmar-saida", methods=["POST"])
def confirmar_saida_navio(agendamento_id):
    blocked = login_required()
    if blocked:
        return blocked
    if not usuario_e_administrador_portuario():
        abort(403)

    agendamento = Agendamento.query.filter_by(
        id=agendamento_id, codigo_porto=session.get("porto")
    ).first_or_404()
    if passagem_pelo_porto_concluida(agendamento):
        flash("Este navio já está no histórico.", "info")
        return redirect(url_for("calendario"))

    decisao = (request.form.get("decisao") or "").strip()
    data_calendario = None
    if decisao == "saiu":
        agendamento.status = "Concluído"
        agendamento.concluido_em = datetime.now()
        agendamento.verificar_saida_adiada_ate = None
        flash("Saída confirmada. O navio foi movido para o histórico.", "success")
    elif decisao == "adiar":
        uma_semana = timedelta(days=7)
        for campo in ("eta", "etb", "ets", "gate_cutoff", "cargo_doc_cutoff"):
            valor = getattr(agendamento, campo)
            if valor:
                setattr(agendamento, campo, valor + uma_semana)
        agendamento.previsao = agendamento.eta or (agendamento.previsao + uma_semana)
        agendamento.status = status_por_previsao(agendamento.previsao)
        agendamento.concluido_em = None
        # Datas antigas ainda podem estar vencidas depois de somar sete dias.
        # Aguardar até a próxima semana para mostrar a pergunta novamente.
        agendamento.verificar_saida_adiada_ate = datetime.now() + uma_semana
        data_calendario = agendamento.eta or agendamento.previsao
        flash("Agendamento adiado em uma semana. A nova janela já aparece no calendário.", "success")
    else:
        abort(400)

    db.session.commit()
    if data_calendario:
        return redirect(url_for("calendario", ano=data_calendario.year, mes=data_calendario.month))
    return redirect(url_for("calendario"))


@app.route("/agendamentos/<agendamento_id>/concluir", methods=["POST"])
def concluir_agendamento(agendamento_id):
    blocked = login_required()
    if blocked:
        return blocked
    if not usuario_e_administrador_portuario():
        abort(403)

    agendamento = Agendamento.query.filter_by(
        id=agendamento_id, codigo_porto=session.get("porto")
    ).first_or_404()
    agendamento.status = "Concluído"
    agendamento.concluido_em = datetime.now()
    db.session.commit()
    flash("Saída confirmada. A carga foi movida para o histórico.", "success")
    return redirect(url_for("calendario"))


@app.route("/agendamentos/<agendamento_id>/status", methods=["POST"])
def atualizar_status_agendamento(agendamento_id):
    blocked = login_required()
    if blocked:
        return blocked
    if not usuario_e_administrador_portuario():
        abort(403)

    status = (request.form.get("status") or "").strip()
    status_permitidos = {"Agendado", "Atrasado", "No porto", "Concluído"}
    if status not in status_permitidos:
        abort(400)

    agendamento = Agendamento.query.filter_by(
        id=agendamento_id, codigo_porto=session.get("porto")
    ).first_or_404()
    agendamento.status = status
    if status == "Concluído":
        agendamento.concluido_em = datetime.now()
    else:
        agendamento.concluido_em = None
    db.session.commit()
    flash("Status da embarcação atualizado.", "success")
    data = agendamento.eta or agendamento.previsao
    return redirect(url_for("calendario", ano=data.year, mes=data.month))


# Cadastro de novo agendamento por responsavel com Codigo de Gestao
@app.route("/agendamentos", methods=["POST"])
def novo_agendamento():
    blocked = login_required()
    if blocked:
        return blocked
    if not usuario_e_administrador_portuario():
        abort(403)

    erro = criar_agendamento(request.form)
    if erro:
        flash(erro, "error")
    else:
        flash("Agendamento criado com sucesso.", "success")

    try:
        previsao = datetime.fromisoformat(request.form.get("eta") or "")
        return redirect(url_for("calendario", ano=previsao.year, mes=previsao.month))
    except ValueError:
        return redirect(url_for("calendario"))


# Execucao local do servidor Flask
if __name__ == "__main__":
    # Evita que cada salvamento de arquivo reinicie o servidor e interrompa
    # a navegação ativa durante a edição local.
    app.run(debug=True, use_reloader=False)
