# Importacao para gerar identificadores unicos
from datetime import datetime
from uuid import uuid4

# Importacao do SQLAlchemy usado pelo Flask
from flask_sqlalchemy import SQLAlchemy

# Instancia principal do banco de dados
db = SQLAlchemy()


# Geracao de ID unico para registros do sistema
def get_uuid():
    return uuid4().hex


def get_short_identifier():
    return uuid4().hex[:8].upper()


# Modelo de usuarios e funcionarios do porto
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(32), primary_key=True, default=get_uuid)
    nome_completo = db.Column(db.String(200), nullable=False)
    data_nascimento = db.Column(db.String(20), nullable=False)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.Text, nullable=False)
    identificacao_interna = db.Column(db.String(20), nullable=False, unique=True, default=get_short_identifier)
    deve_trocar_senha = db.Column(db.Boolean, nullable=False, default=False)
    email = db.Column(db.String(254), nullable=True)
    codigo_porto = db.Column(db.String(50), nullable=False)
    cargo = db.Column(db.String(120), nullable=True)
    poder_maior = db.Column(db.Boolean, nullable=False, default=False)
    avatar_path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=True, default=datetime.now)
    last_access_at = db.Column(db.DateTime, nullable=True)
    status_aprovacao = db.Column(db.String(20), nullable=False, default="pendente")


class HistoricoGerenciamento(db.Model):
    __tablename__ = "historico_gerenciamento"

    id = db.Column(db.String(32), primary_key=True, default=get_uuid)
    ator_id = db.Column(db.String(32), nullable=True)
    ator_nome = db.Column(db.String(200), nullable=False)
    funcionario_id = db.Column(db.String(32), nullable=True)
    funcionario_nome = db.Column(db.String(200), nullable=False)
    codigo_porto = db.Column(db.String(20), nullable=False)
    acao = db.Column(db.String(40), nullable=False)
    detalhes = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.now)


# Modelo de portos cadastrados no sistema
class Porto(db.Model):
    __tablename__ = "portos"

    id = db.Column(db.String(32), primary_key=True, default=get_uuid)
    nome_porto = db.Column(db.String(200), nullable=False)
    nome_rio = db.Column(db.String(200), nullable=False)
    codigo_portuario = db.Column(db.String(20), unique=True, nullable=False)
    cidade = db.Column(db.String(120), nullable=False)
    estado = db.Column(db.String(120), nullable=False)
    latitude = db.Column(db.String(50), nullable=False)
    longitude = db.Column(db.String(50), nullable=False)
    ponto_quilometrico = db.Column(db.String(50), nullable=False)
    margem = db.Column(db.String(30), nullable=False)
    tipo_porto = db.Column(db.String(50), nullable=False)
    codigo_poder_maior = db.Column(db.String(20), unique=True, nullable=True)
    administrador_user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    administrador_username = db.Column(db.String(100), nullable=True)


# Modelo de agendamentos operacionais do calendario
class Agendamento(db.Model):
    __tablename__ = "agendamentos"

    id = db.Column(db.String(32), primary_key=True, default=get_uuid)
    codigo_porto = db.Column(db.String(20), nullable=False)
    tipo_agendamento = db.Column(db.String(50), nullable=False)
    tipo_fluxo = db.Column(db.String(20), nullable=False)
    tipo_carga = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(80), nullable=False)
    verificar_saida_adiada_ate = db.Column(db.DateTime, nullable=True)
    concluido_em = db.Column(db.DateTime, nullable=True)
    previsao = db.Column(db.DateTime, nullable=False)
    bandeira = db.Column(db.String(80), nullable=False)
    peso_bruto = db.Column(db.String(80), nullable=False)
    observacao = db.Column(db.Text, nullable=False)
    dimensoes = db.Column(db.String(120), nullable=False)
    tipo_navio = db.Column(db.String(120), nullable=True)
    fcl_lcl = db.Column(db.String(20), nullable=True)
    temperatura = db.Column(db.String(80), nullable=True)
    capacidade_navio = db.Column(db.String(120), nullable=True)
    plano_carga = db.Column(db.Text, nullable=True)
    eta = db.Column(db.DateTime, nullable=True)
    etb = db.Column(db.DateTime, nullable=True)
    ets = db.Column(db.DateTime, nullable=True)
    nome_embarcacao = db.Column(db.String(160), nullable=True)
    numero_viagem = db.Column(db.String(80), nullable=True)
    armador = db.Column(db.String(160), nullable=True)
    calado = db.Column(db.String(80), nullable=True)
    gate_cutoff = db.Column(db.DateTime, nullable=True)
    cargo_doc_cutoff = db.Column(db.DateTime, nullable=True)
    portos_escala = db.Column(db.Text, nullable=True)
    criado_por = db.Column(db.String(100), nullable=False)


# Modelo de notificacoes geradas por mudancas no calendario
class NotificacaoCalendario(db.Model):
    __tablename__ = "notificacoes_calendario"

    id = db.Column(db.String(32), primary_key=True, default=get_uuid)
    codigo_porto = db.Column(db.String(20), nullable=False)
    titulo = db.Column(db.String(160), nullable=False)
    mensagem = db.Column(db.Text, nullable=False)
    criado_por = db.Column(db.String(100), nullable=False)
    criado_em = db.Column(db.DateTime, nullable=False)


class LeituraNotificacaoCalendario(db.Model):
    __tablename__ = "leituras_notificacoes_calendario"

    id = db.Column(db.String(32), primary_key=True, default=get_uuid)
    notificacao_id = db.Column(db.String(32), db.ForeignKey("notificacoes_calendario.id"), nullable=False)
    usuario = db.Column(db.String(100), nullable=False)
    lida_em = db.Column(db.DateTime, nullable=False, default=datetime.now)
    __table_args__ = (db.UniqueConstraint("notificacao_id", "usuario", name="uq_leitura_notificacao_usuario"),)
