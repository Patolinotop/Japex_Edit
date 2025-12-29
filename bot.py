import os
import re
import json
import asyncio
import datetime
from typing import Dict, List, Optional, Set, Tuple, Any

import discord
from dotenv import load_dotenv
from openai import OpenAI

# ================== ENV ==================
load_dotenv()
TOKEN_DISCORD = os.getenv("DISCORD_BOT_TOKEN")
CHAVE_OPENAI = os.getenv("OPENAI_API_KEY")

if not TOKEN_DISCORD or not CHAVE_OPENAI:
    raise SystemExit("faltou DISCORD_BOT_TOKEN ou OPENAI_API_KEY no .env")

openai = OpenAI(api_key=CHAVE_OPENAI)

# ================== PATHS ==================
PASTA_ATUAL = os.path.dirname(os.path.abspath(__file__))
CAMINHO_DADOS = os.path.join(PASTA_ATUAL, "dados.txt")
CAMINHO_ORDENS = os.path.join(PASTA_ATUAL, "ordens.txt")
CAMINHO_IGNORE = os.path.join(PASTA_ATUAL, "ignorar.txt")
CAMINHO_SILENCIO = os.path.join(PASTA_ATUAL, "silencio.flag")

# ================== CONFIG / IDS ==================
JAPEX_ID = 1331505963622076476  # Fundador

# Histórico por canal (buffer), mas contexto usa só 3 msgs filtradas
HISTORICO: Dict[int, List[dict]] = {}
MAX_MSGS_CONTEXT = 3

# Delay/cooldown anti-abuso
MIN_DELAY_SECONDS = 1.6
USER_COOLDOWN_SECONDS = 2.0
_last_user_action: Dict[int, float] = {}

# ================== ROLES / PATENTES ==================
CHEFOES_POR_NOME = {
    "lalomaio": "Criador do Exército",
    "santiago": "Chefe da Administração",
}

# ordem menor = patente mais alta
PATENTES = [
    ("[S-Cmdt]", "Sub Comandante", 2),
    ("[MR]", "Marechal", 3),
    ("[Gen-Ex]", "General do Exército", 4),
    ("[Gen-Div]", "General de Divisão", 5),
    ("[Gen-B]", "General de Brigada", 6),
    ("[Cel]", "Coronel", 7),
    ("[Ten-Cel]", "Tenente-coronel", 8),
    ("[Maj]", "Major", 9),
    ("[Cap]", "Capitão", 10),
    ("[1°Ten]", "Primeiro Tenente", 11),
    ("[2°Ten]", "Segundo Tenente", 12),
    ("[Asp]", "Aspirante", 13),
    ("[ST]", "Subtenente", 14),
    ("[1°Sgt]", "Primeiro Sargento", 15),
    ("[2°Sgt]", "Segundo Sargento", 16),
    ("[3°Sgt]", "Segundo Sargento", 16),
    ("[3°Sgt]", "Terceiro Sargento", 17),
    ("[Cb]", "Cabo", 18),
    ("[Sld]", "Soldado", 19),
    ("[Rct]", "Recruta", 20),
]

# ================== DISCORD ==================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

cliente = discord.Client(intents=intents)
ocupado = asyncio.Lock()

# ================== UTIL ==================
def normalizar_espacos(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def is_japex(user_id: int) -> bool:
    return user_id == JAPEX_ID

def esta_silenciado() -> bool:
    return os.path.exists(CAMINHO_SILENCIO)

def set_silencio(on: bool) -> None:
    try:
        if on:
            with open(CAMINHO_SILENCIO, "w", encoding="utf-8") as f:
                f.write("1")
        else:
            if os.path.exists(CAMINHO_SILENCIO):
                os.remove(CAMINHO_SILENCIO)
    except:
        pass

async def respeitar_delay_e_cooldown(user_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    if not is_japex(user_id):
        last = _last_user_action.get(user_id, 0.0)
        if (now - last) < USER_COOLDOWN_SECONDS:
            return False
        _last_user_action[user_id] = now
        await asyncio.sleep(MIN_DELAY_SECONDS)
    else:
        await asyncio.sleep(0.6)
    return True

# ================== IGNORADOS ==================
def carregar_ignorados() -> Set[int]:
    s: Set[int] = set()
    try:
        if not os.path.exists(CAMINHO_IGNORE):
            return s
        with open(CAMINHO_IGNORE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s.add(int(line))
                except:
                    pass
    except:
        pass
    return s

def salvar_ignorados(s: Set[int]) -> None:
    try:
        with open(CAMINHO_IGNORE, "w", encoding="utf-8") as f:
            for uid in sorted(s):
                f.write(str(uid) + "\n")
    except:
        pass

IGNORADOS: Set[int] = carregar_ignorados()

# ================== ORDENS PERSISTENTES ==================
def carregar_ordens() -> str:
    try:
        if not os.path.exists(CAMINHO_ORDENS):
            return ""
        with open(CAMINHO_ORDENS, "r", encoding="utf-8") as f:
            return f.read().strip()
    except:
        return ""

def salvar_ordens(texto: str) -> None:
    try:
        with open(CAMINHO_ORDENS, "w", encoding="utf-8") as f:
            f.write(texto.strip())
    except:
        pass

def limitar_ordens(texto: str, max_chars: int = 420) -> str:
    texto = normalizar_espacos(texto)
    if len(texto) <= max_chars:
        return texto
    return texto[-max_chars:].strip()

def adicionar_ordem(nova: str) -> None:
    nova = normalizar_espacos(nova)
    if not nova:
        return
    atual = carregar_ordens()
    combinado = (atual + "\n" + f"- {nova}").strip() if atual else f"- {nova}"
    combinado = limitar_ordens(combinado, max_chars=420)
    salvar_ordens(combinado)

def limpar_ordens() -> None:
    try:
        if os.path.exists(CAMINHO_ORDENS):
            os.remove(CAMINHO_ORDENS)
    except:
        pass

# ================== ROLES (pra IA escolher vocativo) ==================
def listar_roles_curto(member: discord.Member, max_roles: int = 10) -> List[str]:
    roles = []
    for r in getattr(member, "roles", []):
        if not r or not r.name:
            continue
        if r.is_default():  # @everyone
            continue
        roles.append(r.name.strip())
    # mais importantes primeiro: tenta colocar patentes na frente
    def score(nome: str) -> int:
        for tag, titulo, ordem in PATENTES:
            if tag in nome:
                return ordem
        return 999
    roles.sort(key=score)
    return roles[:max_roles]

def melhor_patente_guess(member: discord.Member) -> Optional[str]:
    melhor = None
    melhor_ordem = 999
    for role in getattr(member, "roles", []):
        rnome = role.name or ""
        for tag, titulo, ordem in PATENTES:
            if tag in rnome and ordem < melhor_ordem:
                melhor = titulo
                melhor_ordem = ordem
    if melhor:
        return melhor
    for role in getattr(member, "roles", []):
        rnome = role.name or ""
        for tag, titulo, ordem in PATENTES:
            if titulo.lower() in rnome.lower() and ordem < melhor_ordem:
                melhor = titulo
                melhor_ordem = ordem
    return melhor

# ================== DADOS.TXT (BUSCA DE BLOCO CURTO) ==================
STOPWORDS = {
    "a","o","os","as","de","do","da","dos","das","e","em","no","na","nos","nas",
    "um","uma","uns","umas","para","por","com","sem","que","é","ser","se","ao",
    "à","às","ou","como","mais","menos","muito","pouco","já","não","sim","nao",
    "sobre","isso","isto","aquele","aquela","aquilo","meu","minha","seu","sua",
    "pra","pro","pq","porque"
}
_dados_cache = {"mtime": None, "blocos": []}  # List[Tuple[titulo, texto, tokens]]

def _tokenizar(s: str) -> Set[str]:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9áàâãéèêíìîóòôõúùûç°\s]", " ", s, flags=re.IGNORECASE)
    parts = [p for p in s.split() if p and p not in STOPWORDS and len(p) > 2]
    return set(parts)

def carregar_blocos_dados() -> List[Tuple[str, str, Set[str]]]:
    try:
        if not os.path.exists(CAMINHO_DADOS):
            return []
        mtime = os.path.getmtime(CAMINHO_DADOS)
        if _dados_cache["mtime"] == mtime and _dados_cache["blocos"]:
            return _dados_cache["blocos"]

        with open(CAMINHO_DADOS, "r", encoding="utf-8") as f:
            raw = f.read().replace("\r\n", "\n").strip()
        if not raw:
            return []

        partes = re.split(r"(?m)^\s*##\s+", raw)
        blocos: List[Tuple[str, str, Set[str]]] = []

        if partes and not raw.lstrip().startswith("##"):
            titulo = "GERAL"
            texto = partes[0].strip()
            toks = _tokenizar(titulo + " " + texto)
            blocos.append((titulo, texto, toks))
            partes = partes[1:]

        for p in partes:
            p = p.strip()
            if not p:
                continue
            linhas = p.split("\n", 1)
            titulo = normalizar_espacos(linhas[0])[:60] if linhas else "BLOCO"
            texto = linhas[1].strip() if len(linhas) > 1 else ""
            texto = re.sub(r"\n{3,}", "\n\n", texto).strip()
            toks = _tokenizar(titulo + " " + texto)
            blocos.append((titulo, texto, toks))

        _dados_cache["mtime"] = mtime
        _dados_cache["blocos"] = blocos
        return blocos
    except:
        return []

def buscar_contexto_dados(pergunta: str, max_chars: int = 850) -> str:
    blocos = carregar_blocos_dados()
    if not blocos:
        return ""
    q_tokens = _tokenizar(pergunta)
    if not q_tokens:
        return ""
    melhor_score = 0
    melhor: Optional[Tuple[str, str]] = None
    for titulo, texto, toks in blocos:
        inter = len(q_tokens.intersection(toks))
        if inter > melhor_score:
            melhor_score = inter
            melhor = (titulo, texto)
    if not melhor or melhor_score < 2:
        return ""
    titulo, texto = melhor
    contexto = normalizar_espacos(f"[{titulo}] {texto}")
    if len(contexto) > max_chars:
        contexto = contexto[:max_chars].rstrip() + "..."
    return contexto

# ================== HISTÓRICO (3 msgs filtradas: autor ou Japex) ==================
def adicionar_historico(channel_id: int, author_id: int, role: str, content: str) -> None:
    content = normalizar_espacos(content)
    if not content:
        return
    if channel_id not in HISTORICO:
        HISTORICO[channel_id] = []
    HISTORICO[channel_id].append({"author_id": author_id, "role": role, "content": content})
    HISTORICO[channel_id] = HISTORICO[channel_id][-60:]

def historico_filtrado(channel_id: int, user_id: int) -> List[dict]:
    hist = HISTORICO.get(channel_id, [])
    filtrado = [m for m in hist if (m["author_id"] == user_id or m["author_id"] == JAPEX_ID)]
    ultimas = filtrado[-MAX_MSGS_CONTEXT:]
    return [{"role": m["role"], "content": m["content"]} for m in ultimas]

# ================== DISCORD ACTIONS ==================
async def mutar(member: discord.Member, segundos: int) -> bool:
    try:
        ate = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=segundos)
        await member.edit(timed_out_until=ate, reason="moderação")
        return True
    except:
        return False

async def desmutar(member: discord.Member) -> bool:
    try:
        await member.edit(timed_out_until=None, reason="moderação")
        return True
    except:
        return False

async def banir(member: discord.Member) -> bool:
    try:
        if member.guild:
            await member.guild.ban(member, reason="moderação", delete_message_seconds=0)
            return True
        return False
    except:
        return False

def relatorio_mutado(nome: str, dur: int) -> str:
    return f"Mutado: {nome} | Duração: {dur}s"

def relatorio_banido(nome: str) -> str:
    return f"Banido: {nome} | Duração: permanente"

# ================== OPENAI: DECISOR DE PUNIÇÃO (RÍGIDO, MAS COM FREIO) ==================
ALLOWED_DISCIPLINE = ["none", "mute_60", "mute_300", "mute_900", "ban"]

def decidir_punicao_disciplina_sync(payload: dict) -> str:
    """
    Decide punição com base no texto + contexto mínimo.
    Deve ser rígido contra: desrespeito, assédio, difamação/calúnia, ameaça, ódio.
    Mas NÃO pode punir se não houver evidência clara.
    """
    prompt = (
        "Você é um moderador rígido e justo de um servidor.\n"
        "Escolha UMA ação: none, mute_60, mute_300, mute_900, ban.\n"
        "Critérios:\n"
        "- Puna se houver evidência clara de: desrespeito direto, assédio, humilhação, provocação pesada, difamação/calúnia (acusação grave sem base), ameaça, discurso de ódio.\n"
        "- Se o bot foi mencionado e o texto contém desrespeito/insubordinação clara, aplique pelo menos mute_60.\n"
        "- Se for moderado: mute_300. Grave/ameaça: mute_900. Extremamente grave: ban.\n"
        "- Se não estiver claro, escolha none.\n"
        "Responda APENAS com JSON: {\"action\":\"...\"}\n"
        f"PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:1800]}"
    )
    r = openai.responses.create(
        model="gpt-4o",
        input=[
            {"role": "system", "content": "Responda apenas JSON válido."},
            {"role": "user", "content": prompt}
        ],
        max_output_tokens=80,
        temperature=0.1
    )
    raw = (r.output_text or "").strip()
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        return "none"
    try:
        obj = json.loads(m.group(0))
        act = str(obj.get("action", "none")).strip()
        return act if act in ALLOWED_DISCIPLINE else "none"
    except:
        return "none"

async def decidir_punicao_disciplina(payload: dict) -> str:
    try:
        return await asyncio.wait_for(asyncio.to_thread(decidir_punicao_disciplina_sync, payload), timeout=10)
    except:
        return "none"

def duracao_por_action(act: str) -> int:
    if act == "mute_60":
        return 60
    if act == "mute_300":
        return 300
    if act == "mute_900":
        return 900
    return 0

# ================== OPENAI: INTÉRPRETE DE ORDEM DO JAPEX ==================
def _remover_mencao_bot(texto: str) -> str:
    if cliente.user:
        texto = texto.replace(cliente.user.mention, "")
    return normalizar_espacos(texto)

def interpretar_ordem_japex_sync(texto: str, mentions: List[dict], reply_text: str) -> dict:
    schema = {"action": "none", "target_user_id": None, "duration_seconds": None, "order_text": None}
    prompt = (
        "Interprete a mensagem do Senhor Fundador como uma ordem.\n"
        "Responda APENAS com JSON válido.\n"
        "Ações permitidas: mute, unmute, ban, ignore, unignore, silence_on, silence_off, add_order, reset_orders, none.\n"
        "Regras:\n"
        "- Se for punir alguém, escolha o alvo APENAS dentre MENTIONS.\n"
        "- duration_seconds: inteiro (padrão 60) quando action=mute.\n"
        "- add_order: coloque texto em order_text (máx 300 chars).\n"
        f"MENSAGEM: {texto}\n"
        f"MENTIONS: {json.dumps(mentions, ensure_ascii=False)}\n"
        f"REPLY_TEXT: {reply_text}\n"
        f"JSON_BASE: {json.dumps(schema)}"
    )
    r = openai.responses.create(
        model="gpt-4o",
        input=[
            {"role": "system", "content": "Responda apenas JSON válido, sem texto extra."},
            {"role": "user", "content": prompt}
        ],
        max_output_tokens=140,
        temperature=0.1
    )
    raw = (r.output_text or "").strip()
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        return schema
    try:
        obj = json.loads(m.group(0))
        action = str(obj.get("action", "none")).strip()
        if action not in ["mute","unmute","ban","ignore","unignore","silence_on","silence_off","add_order","reset_orders","none"]:
            action = "none"

        target = obj.get("target_user_id", None)
        if isinstance(target, int):
            target_user_id = target
        elif isinstance(target, str) and target.isdigit():
            target_user_id = int(target)
        else:
            target_user_id = None

        dur = obj.get("duration_seconds", None)
        if isinstance(dur, (int, float)):
            duration_seconds = int(max(1, min(86400, dur)))
        elif isinstance(dur, str) and dur.isdigit():
            duration_seconds = int(max(1, min(86400, int(dur))))
        else:
            duration_seconds = None

        order_text = obj.get("order_text", None)
        if order_text is not None:
            order_text = normalizar_espacos(str(order_text))[:300]

        return {"action": action, "target_user_id": target_user_id, "duration_seconds": duration_seconds, "order_text": order_text}
    except:
        return schema

async def interpretar_ordem_japex(texto: str, mentions: List[dict], reply_text: str) -> dict:
    try:
        return await asyncio.wait_for(asyncio.to_thread(interpretar_ordem_japex_sync, texto, mentions, reply_text), timeout=12)
    except:
        return {"action": "none", "target_user_id": None, "duration_seconds": None, "order_text": None}

# ================== CHAT GPT (resposta curta) ==================
PERSONA_BASE = (
    "Postura militar, disciplina e respostas curtas.\n"
    "Regra crítica: SOMENTE se author_id == fundador_id você pode usar 'Senhor Fundador' ou 'Senhor Japex'.\n"
    "Se author_id != fundador_id: é PROIBIDO chamar o usuário de Fundador/Japex.\n"
    "Escolha um vocativo curto e coerente com os cargos do autor (se houver patente, use ela; senão, use o nome).\n"
    "Português correto; sem gíria pesada; sem tom robótico.\n"
    "Não puxe assunto e não faça perguntas.\n"
    "Nunca diga que é IA.\n"
    "Assuntos de alistamento/treinamento/vedação são do JOGO do servidor.\n"
)

def montar_system(author_id: int, roles: List[str], best_guess: str, display_name: str, contexto_dados: str) -> str:
    ordens = carregar_ordens()
    # limite de tokens via instrução curta
    extra = (
        f"author_id={author_id} fundador_id={JAPEX_ID}. "
        f"display_name={display_name}. "
        f"roles={roles}. "
        f"best_guess={best_guess}. "
        "Responda em no máximo 40 tokens (ou 60 se pedirem explicação/texto). "
        "Use vocativo no início, curto, sem @ e sem colchetes."
    )
    if ordens:
        extra += " ORDENS PERMANENTES DO FUNDADOR: " + ordens
    if contexto_dados:
        extra += " BASE DO SERVIDOR: " + contexto_dados
    return PERSONA_BASE + " " + extra

def quer_texto(texto: str) -> bool:
    t = (texto or "").lower()
    return any(g in t for g in [
        "faça um texto", "faz um texto", "texto gramatical",
        "explique", "explica", "detalhe", "detalha",
        "passo a passo", "redação", "redacao"
    ])

def chat_sync(mensagens: List[dict], max_tokens: int) -> str:
    r = openai.responses.create(
        model="gpt-4o",
        input=mensagens,
        max_output_tokens=max_tokens,
        temperature=0.6,
    )
    msg = normalizar_espacos((r.output_text or "").strip())
    return msg if msg else "Entendido."

async def gerar_resposta(texto: str, member: discord.Member, channel_id: int) -> str:
    usar_texto = quer_texto(texto)
    max_tokens = 95 if usar_texto else 55

    roles = listar_roles_curto(member, max_roles=10)
    guess = melhor_patente_guess(member) or member.display_name
    contexto = buscar_contexto_dados(texto, max_chars=850)

    system = montar_system(member.id, roles, guess, member.display_name, contexto)
    msgs: List[dict] = [{"role": "system", "content": system}]
    msgs.extend(historico_filtrado(channel_id, member.id))
    msgs.append({"role": "user", "content": texto})

    try:
        return await asyncio.wait_for(asyncio.to_thread(chat_sync, msgs, max_tokens), timeout=12)
    except Exception as e:
        print("ERRO OPENAI CHAT:", repr(e))
        # Fallback: nunca retorna "Negado." aqui
        voc = "Senhor Fundador" if is_japex(member.id) else (melhor_patente_guess(member) or member.display_name)
        return f"Entendido, {voc}."

# ================== REPLY: PEGA MSG REFERENCIADA ==================
async def pegar_mensagem_referenciada(msg: discord.Message) -> Optional[discord.Message]:
    try:
        if not msg.reference:
            return None
        if isinstance(msg.reference.resolved, discord.Message):
            return msg.reference.resolved
        if msg.reference.message_id and msg.channel:
            return await msg.channel.fetch_message(msg.reference.message_id)
    except:
        return None
    return None

# ================== EVENTS ==================
@cliente.event
async def on_ready():
    print("bot ligado (rigidez + Japex only + ordem via IA + vocativo por cargos + corrigido ban warning)")

@cliente.event
async def on_message(mensagem: discord.Message):
    if mensagem.author.bot:
        return
    if not isinstance(mensagem.author, discord.Member):
        return

    # Ignorados: não responde (nunca ignora Japex)
    if (mensagem.author.id in IGNORADOS) and (not is_japex(mensagem.author.id)):
        return

    # Silêncio: ninguém fala; Japex só pode mandar ordens quando mencionar o bot
    if esta_silenciado() and (not is_japex(mensagem.author.id)):
        return

    # Só age se o bot for mencionado
    if cliente.user not in mensagem.mentions:
        return

    # trava anti-spam geral
    if ocupado.locked():
        return

    if not await respeitar_delay_e_cooldown(mensagem.author.id):
        return

    async with ocupado:
        # 1) Japex: ordem via IA (sem comandos pré-definidos)
        if is_japex(mensagem.author.id):
            texto_ordem = _remover_mencao_bot(mensagem.content)

            mentions = []
            for m in mensagem.mentions:
                if cliente.user and m.id == cliente.user.id:
                    continue
                if isinstance(m, discord.Member):
                    mentions.append({"user_id": m.id, "display_name": m.display_name})

            ref = await pegar_mensagem_referenciada(mensagem)
            reply_text = normalizar_espacos(ref.content)[:400] if ref and ref.content else ""

            ordem = await interpretar_ordem_japex(texto_ordem, mentions, reply_text)
            act = ordem.get("action", "none")
            target_id = ordem.get("target_user_id", None)
            duration = ordem.get("duration_seconds", None)
            order_text = ordem.get("order_text", None)

            if act == "reset_orders":
                limpar_ordens()
                await mensagem.channel.send("Sim, Senhor Fundador.")
                return

            if act == "silence_on":
                set_silencio(True)
                adicionar_ordem("Ficar em silêncio até nova ordem do Senhor Fundador.")
                await mensagem.channel.send("Sim, Senhor Fundador.")
                return

            if act == "silence_off":
                set_silencio(False)
                await mensagem.channel.send("Sim, Senhor Fundador.")
                return

            if act == "add_order" and order_text:
                adicionar_ordem(order_text)
                await mensagem.channel.send("Sim, Senhor Fundador. Como ordena.")
                return

            if act in ["ignore", "unignore"]:
                if target_id:
                    if act == "ignore":
                        IGNORADOS.add(int(target_id))
                    else:
                        IGNORADOS.discard(int(target_id))
                    salvar_ignorados(IGNORADOS)
                await mensagem.channel.send("Sim, Senhor Fundador.")
                return

            if act in ["mute", "unmute", "ban"]:
                if not target_id:
                    await mensagem.channel.send("Sim, Senhor Fundador.")
                    return

                alvo = mensagem.guild.get_member(int(target_id)) if mensagem.guild else None
                if not alvo:
                    await mensagem.channel.send("Negado.")
                    return

                if act == "unmute":
                    ok = await desmutar(alvo)
                    await mensagem.channel.send(f"Desmutado: {alvo.display_name}" if ok else "Negado.")
                    return

                if act == "mute":
                    dur = int(duration) if isinstance(duration, int) else 60
                    dur = max(1, min(86400, dur))
                    ok = await mutar(alvo, dur)
                    await mensagem.channel.send(relatorio_mutado(alvo.display_name, dur) if ok else "Negado.")
                    return

                if act == "ban":
                    ok = await banir(alvo)
                    await mensagem.channel.send(relatorio_banido(alvo.display_name) if ok else "Negado.")
                    return

            # se não foi ordem executável, cai pra conversa normal (Japex conversando)
            # continua...

        # 2) Usuário comum: disciplina por IA (mais rígido)
        # Se for reply com mention: pode punir autor da mensagem original (difamação/desrespeito etc.)
        ref = await pegar_mensagem_referenciada(mensagem)
        if ref and not ref.author.bot and mensagem.guild:
            alvo_ref = mensagem.guild.get_member(ref.author.id)
            if alvo_ref and (not is_japex(alvo_ref.id)):
                payload = {
                    "mode": "reply_report",
                    "bot_was_mentioned": True,
                    "reporter_id": mensagem.author.id,
                    "target_id": alvo_ref.id,
                    "target_message": normalizar_espacos(ref.content or "")[:900],
                    "reporter_message": normalizar_espacos(_remover_mencao_bot(mensagem.content))[:500],
                }
                act = await decidir_punicao_disciplina(payload)
                if act != "none":
                    if act == "ban":
                        ok = await banir(alvo_ref)
                        await mensagem.channel.send(relatorio_banido(alvo_ref.display_name) if ok else "Negado.")
                        return
                    dur = duracao_por_action(act)
                    ok = await mutar(alvo_ref, dur)
                    await mensagem.channel.send(relatorio_mutado(alvo_ref.display_name, dur) if ok else "Negado.")
                    return

        # 3) Disciplina do próprio autor (se marcou o bot desrespeitando, pune)
        if not is_japex(mensagem.author.id):
            texto_user = normalizar_espacos(_remover_mencao_bot(mensagem.content))[:900]
            payload = {
                "mode": "direct_mention",
                "bot_was_mentioned": True,
                "author_id": mensagem.author.id,
                "author_name": mensagem.author.display_name,
                "text": texto_user,
            }
            act = await decidir_punicao_disciplina(payload)
            if act != "none":
                if act == "ban":
                    ok = await banir(mensagem.author)
                    await mensagem.channel.send(relatorio_banido(mensagem.author.display_name) if ok else "Negado.")
                    return
                dur = duracao_por_action(act)
                ok = await mutar(mensagem.author, dur)
                await mensagem.channel.send(relatorio_mutado(mensagem.author.display_name, dur) if ok else "Negado.")
                return

        # 4) Conversa normal
        texto = _remover_mencao_bot(mensagem.content)
        if not texto:
            return

        async with mensagem.channel.typing():
            resposta = await gerar_resposta(texto, mensagem.author, mensagem.channel.id)

        adicionar_historico(mensagem.channel.id, mensagem.author.id, "user", texto)
        adicionar_historico(mensagem.channel.id, mensagem.author.id, "assistant", resposta)

        await mensagem.reply(resposta)

cliente.run(TOKEN_DISCORD)
