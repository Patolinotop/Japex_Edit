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

# Delay/cooldown anti-abuso (não mexe no lock global)
MIN_DELAY_SECONDS = 1.4
USER_COOLDOWN_SECONDS = 2.0
_last_user_action: Dict[int, float] = {}

# ================== PATENTES (ordem menor = patente mais alta) ==================
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

# LOCK GLOBAL: enquanto processa 1 menção, ignora o resto
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
        await asyncio.sleep(0.4)
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
                if line.isdigit():
                    s.add(int(line))
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
    salvar_ordens(limitar_ordens(combinado, max_chars=420))

def limpar_ordens() -> None:
    try:
        if os.path.exists(CAMINHO_ORDENS):
            os.remove(CAMINHO_ORDENS)
    except:
        pass

# ================== ROLES / PATENTE ==================
def rank_order(member: discord.Member) -> Optional[int]:
    """
    Retorna o menor 'ordem' encontrado nas roles do member, ou None se não achar.
    Menor = mais alto.
    """
    best = None
    for role in getattr(member, "roles", []):
        rnome = role.name or ""
        for tag, titulo, ordem in PATENTES:
            if tag in rnome:
                if best is None or ordem < best:
                    best = ordem
    if best is not None:
        return best

    # fallback: tenta pelo nome do título
    for role in getattr(member, "roles", []):
        rnome = role.name or ""
        for tag, titulo, ordem in PATENTES:
            if titulo.lower() in rnome.lower():
                if best is None or ordem < best:
                    best = ordem
    return best

def best_patente_title(member: discord.Member) -> Optional[str]:
    """
    Melhor patente (maior) do member, como título sem colchetes.
    """
    best_title = None
    best_ord = 999
    for role in getattr(member, "roles", []):
        rnome = role.name or ""
        for tag, titulo, ordem in PATENTES:
            if tag in rnome and ordem < best_ord:
                best_title = titulo
                best_ord = ordem
    if best_title:
        return best_title
    for role in getattr(member, "roles", []):
        rnome = role.name or ""
        for tag, titulo, ordem in PATENTES:
            if titulo.lower() in rnome.lower() and ordem < best_ord:
                best_title = titulo
                best_ord = ordem
    return best_title

def roles_curto(member: discord.Member, max_roles: int = 10) -> List[str]:
    roles = []
    for r in getattr(member, "roles", []):
        if not r or not r.name:
            continue
        if r.is_default():
            continue
        roles.append(r.name.strip())
    # põe patentes na frente (mais altas primeiro)
    def key(nome: str) -> int:
        for tag, titulo, ordem in PATENTES:
            if tag in nome:
                return ordem
        return 999
    roles.sort(key=key)
    return roles[:max_roles]

def tratamento_padrao(member: discord.Member) -> str:
    """
    Vocativo curto. Se tiver patente, use ela; senão, use nome.
    Japex sempre 'Senhor Japex'.
    """
    if is_japex(member.id):
        return "Senhor Japex"
    pat = best_patente_title(member)
    return pat if pat else member.display_name

def autor_e_superior_do_bot(author: discord.Member, guild: discord.Guild) -> bool:
    """
    Retorna True se author pode mandar no bot:
    - Japex sempre True
    - Se author tiver patente mais alta (ordem menor) que a do bot, True
    """
    if is_japex(author.id):
        return True
    if not guild or not cliente.user:
        return False
    bot_member = guild.get_member(cliente.user.id)
    if not bot_member:
        return False
    a = rank_order(author)
    b = rank_order(bot_member)
    # Se não dá pra comparar, não concede autoridade (seguro)
    if a is None or b is None:
        return False
    return a < b  # menor = mais alto

# ================== DADOS.TXT (BUSCA CURTA) ==================
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

# ================== HISTÓRICO (3 msgs: autor ou Japex) ==================
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

# ================== AÇÕES DISCORD ==================
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

# ================== IA: DECIDE PUNIÇÃO + MOTIVO (CURTO) ==================
ALLOWED_DISCIPLINE = ["none", "mute_60", "mute_300", "mute_900", "ban"]

def decidir_punicao_e_motivo_sync(payload: dict) -> dict:
    """
    Retorna {"action":..., "reason":...} com reason curto (0,5–1 linha).
    Rígido, mas justo: se não estiver claro, none.
    """
    schema = {"action": "none", "reason": ""}

    prompt = (
        "Você é um moderador rígido e justo.\n"
        "Escolha UMA ação: none, mute_60, mute_300, mute_900, ban.\n"
        "Pune com firmeza: calúnia/difamação, desrespeito, assédio, humilhação, ameaça, ódio.\n"
        "Regra especial: desrespeito direto ao Senhor Japex = punição imediata.\n"
        "MAS: se não houver evidência clara no texto, escolha none.\n"
        "O motivo (reason) deve ser curto: 0,5–1 linha, direto, sem textão.\n"
        "Responda APENAS com JSON: {\"action\":\"...\",\"reason\":\"...\"}\n"
        f"PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:2200]}"
    )

    r = openai.responses.create(
        model="gpt-4o",
        input=[
            {"role": "system", "content": "Responda apenas JSON válido."},
            {"role": "user", "content": prompt},
        ],
        max_output_tokens=120,
        temperature=0.1,
    )

    raw = (r.output_text or "").strip()
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        return schema
    try:
        obj = json.loads(m.group(0))
        act = str(obj.get("action", "none")).strip()
        if act not in ALLOWED_DISCIPLINE:
            act = "none"
        reason = normalizar_espacos(str(obj.get("reason", "")))[:140]
        if act == "none":
            reason = ""
        return {"action": act, "reason": reason}
    except:
        return schema

async def decidir_punicao_e_motivo(payload: dict) -> dict:
    try:
        return await asyncio.wait_for(asyncio.to_thread(decidir_punicao_e_motivo_sync, payload), timeout=12)
    except:
        return {"action": "none", "reason": ""}

def duracao_por_action(act: str) -> int:
    return {"mute_60": 60, "mute_300": 300, "mute_900": 900}.get(act, 0)

def relatorio_punicao(action: str, alvo: str, dur: Optional[int], reason: str) -> str:
    if action == "ban":
        return f"Banido: {alvo} | Duração: permanente | Motivo: {reason}"
    if action.startswith("mute_"):
        return f"Mutado: {alvo} | Duração: {dur}s | Motivo: {reason}"
    return ""

# ================== IA: INTERPRETA ORDEM DE SUPERIOR (SEM COMANDO FIXO) ==================
def interpretar_ordem_superior_sync(texto: str, mentions: List[dict], commander_is_japex: bool) -> dict:
    """
    Interpreta ordem livre e retorna ação executável + motivo curto (quando punir).
    """
    schema = {
        "action": "none",               # mute/unmute/ban/ignore/unignore/silence_on/silence_off/add_order/reset_orders/none
        "target_user_id": None,
        "duration_seconds": None,
        "order_text": None,
        "reason": ""
    }

    prompt = (
        "Você interpreta ordens de um superior no servidor.\n"
        "Responda APENAS com JSON.\n"
        "Ações permitidas: mute, unmute, ban, ignore, unignore, silence_on, silence_off, add_order, reset_orders, none.\n"
        "Regras:\n"
        "- Se punir alguém (mute/unmute/ban/ignore/unignore), target_user_id deve vir APENAS da lista MENTIONS.\n"
        "- Se mute e não houver duração, use 60.\n"
        "- reason: se for mute/ban, escreva 0,5–1 linha do motivo.\n"
        "- commander_is_japex indica se a ordem veio do Senhor Japex (pode ser mais direto).\n"
        f"commander_is_japex={commander_is_japex}\n"
        f"MENSAGEM: {texto}\n"
        f"MENTIONS: {json.dumps(mentions, ensure_ascii=False)}\n"
        f"JSON_BASE: {json.dumps(schema, ensure_ascii=False)}"
    )

    r = openai.responses.create(
        model="gpt-4o",
        input=[
            {"role": "system", "content": "Responda apenas JSON válido, sem texto extra."},
            {"role": "user", "content": prompt},
        ],
        max_output_tokens=160,
        temperature=0.1,
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

        reason = normalizar_espacos(str(obj.get("reason", "")))[:140]
        if action not in ["mute", "ban"]:
            reason = ""

        return {
            "action": action,
            "target_user_id": target_user_id,
            "duration_seconds": duration_seconds,
            "order_text": order_text,
            "reason": reason
        }
    except:
        return schema

async def interpretar_ordem_superior(texto: str, mentions: List[dict], commander_is_japex: bool) -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(interpretar_ordem_superior_sync, texto, mentions, commander_is_japex),
            timeout=12,
        )
    except:
        return {"action":"none","target_user_id":None,"duration_seconds":None,"order_text":None,"reason":""}

# ================== IA: CHAT (CONVERSA NORMAL + OPINIÕES) ==================
PERSONA_BASE = (
    "Você é um personagem de servidor militar (do jogo), conversa normal e bem-humorado na medida certa.\n"
    "Pode ter opiniões próprias (inclusive política e sobre membros), sem frescura e sem se recusar por 'neutralidade'.\n"
    "Seja direto, humano, e use gramática boa.\n"
    "Regra crítica de tratamento:\n"
    "- Só se author_id == fundador_id você pode chamar de 'Senhor Japex' e puxar saco de forma formal.\n"
    "- Se author_id != fundador_id: é PROIBIDO chamar o autor de Japex/Fundador.\n"
    "Vocativo:\n"
    "- Prefira vocativo por patente/cargo se existir, senão use o nome.\n"
    "- Nunca faça 'Subtenente, sim'. Faça 'Sim, Subtenente.' quando couber.\n"
    "Não puxe assunto e não faça perguntas.\n"
    "Nunca diga que é IA.\n"
    "Assuntos de alistamento/treinamento/vedação são do JOGO do servidor.\n"
)

def montar_system(author: discord.Member, contexto_dados: str) -> str:
    roles = roles_curto(author, max_roles=10)
    guess = best_patente_title(author) or author.display_name
    ordens = carregar_ordens()

    extra = (
        f"author_id={author.id} fundador_id={JAPEX_ID}. "
        f"display_name={author.display_name}. "
        f"roles={roles}. best_guess={guess}. "
        "Limite: 45 tokens (ou 70 se pedirem explicação/texto). "
        "Comece com um vocativo curto e formal. "
        "Se for resposta de concordância, use 'Sim, <Vocativo>.'"
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
        temperature=0.7,
    )
    msg = normalizar_espacos((r.output_text or "").strip())
    return msg if msg else "Entendido."

async def gerar_resposta(texto: str, author: discord.Member, channel_id: int) -> str:
    usar_texto = quer_texto(texto)
    max_tokens = 95 if usar_texto else 60

    contexto = buscar_contexto_dados(texto, max_chars=850)
    system = montar_system(author, contexto)

    msgs: List[dict] = [{"role": "system", "content": system}]
    msgs.extend(historico_filtrado(channel_id, author.id))
    msgs.append({"role": "user", "content": texto})

    try:
        return await asyncio.wait_for(asyncio.to_thread(chat_sync, msgs, max_tokens), timeout=12)
    except Exception as e:
        print("ERRO OPENAI CHAT:", repr(e))
        # fallback sem “Negado.”
        voc = "Senhor Japex" if is_japex(author.id) else (best_patente_title(author) or author.display_name)
        return f"Entendido, {voc}."

# ================== REPLY: pega mensagem referenciada ==================
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

def remover_mencao_bot(texto: str) -> str:
    if cliente.user:
        texto = texto.replace(cliente.user.mention, "")
    return normalizar_espacos(texto)

# ================== EVENTS ==================
@cliente.event
async def on_ready():
    print("bot ligado (autoridade por patente + Japex bajulado + motivo IA + reply corrigido + lock total)")

@cliente.event
async def on_message(mensagem: discord.Message):
    if mensagem.author.bot:
        return
    if not isinstance(mensagem.author, discord.Member):
        return

    # lock total: se está ocupado, ignora QUALQUER menção até terminar
    if ocupado.locked():
        return

    # só age se foi mencionado
    if cliente.user not in mensagem.mentions:
        return

    # cooldown/delay antes de entrar no lock (reduz spam)
    if not await respeitar_delay_e_cooldown(mensagem.author.id):
        return

    async with ocupado:
        # respeita silêncio: só superiores podem agir (mencionando o bot)
        if esta_silenciado():
            if not autor_e_superior_do_bot(mensagem.author, mensagem.guild):
                return

        # ignorados: não responde (exceto Japex)
        if (mensagem.author.id in IGNORADOS) and (not is_japex(mensagem.author.id)):
            return

        # ---------- 1) Se for SUPERIOR (Japex ou patente mais alta): interpretar ordem ----------
        if autor_e_superior_do_bot(mensagem.author, mensagem.guild):
            texto_ordem = remover_mencao_bot(mensagem.content)

            # mentions possíveis como alvo (exceto o bot)
            mentions = []
            for m in mensagem.mentions:
                if cliente.user and m.id == cliente.user.id:
                    continue
                if isinstance(m, discord.Member):
                    mentions.append({"user_id": m.id, "display_name": m.display_name})

            ordem = await interpretar_ordem_superior(
                texto=texto_ordem,
                mentions=mentions,
                commander_is_japex=is_japex(mensagem.author.id),
            )

            act = ordem.get("action", "none")
            target_id = ordem.get("target_user_id", None)
            duration = ordem.get("duration_seconds", None)
            order_text = ordem.get("order_text", None)
            reason = ordem.get("reason", "")

            # respostas curtas e respeitosas
            superior_voc = "Senhor Japex" if is_japex(mensagem.author.id) else (best_patente_title(mensagem.author) or mensagem.author.display_name)
            ack = "Sim, Senhor Japex." if is_japex(mensagem.author.id) else f"Sim, {superior_voc}."

            if act == "reset_orders":
                limpar_ordens()
                await mensagem.channel.send(ack)
                return

            if act == "silence_on":
                set_silencio(True)
                adicionar_ordem("Ficar em silêncio até nova ordem.")
                await mensagem.channel.send(ack)
                return

            if act == "silence_off":
                set_silencio(False)
                await mensagem.channel.send(ack)
                return

            if act == "add_order" and order_text:
                adicionar_ordem(order_text)
                await mensagem.channel.send(ack)
                return

            if act in ["ignore", "unignore"]:
                if target_id:
                    if act == "ignore":
                        IGNORADOS.add(int(target_id))
                    else:
                        IGNORADOS.discard(int(target_id))
                    salvar_ignorados(IGNORADOS)
                await mensagem.channel.send(ack)
                return

            if act in ["mute", "unmute", "ban"]:
                if not target_id:
                    await mensagem.channel.send(ack)
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
                    if ok:
                        mot = reason if reason else "Conduta inadequada."
                        await mensagem.channel.send(f"Mutado: {alvo.display_name} | Duração: {dur}s | Motivo: {mot}")
                    else:
                        await mensagem.channel.send("Negado.")
                    return

                if act == "ban":
                    ok = await banir(alvo)
                    if ok:
                        mot = reason if reason else "Infração grave."
                        await mensagem.channel.send(f"Banido: {alvo.display_name} | Duração: permanente | Motivo: {mot}")
                    else:
                        await mensagem.channel.send("Negado.")
                    return

            # Se não era ordem executável, cai pra conversa (inclusive superior conversando)
            # (não retorna aqui)

        # ---------- 2) Reply-denúncia: se respondeu uma mensagem e mencionou o bot ----------
        # IMPORTANTE: aqui pune o AUTOR ORIGINAL, nunca o denunciante
        ref = await pegar_mensagem_referenciada(mensagem)
        if ref and not ref.author.bot and mensagem.guild:
            alvo_ref = mensagem.guild.get_member(ref.author.id)
            if alvo_ref and (not is_japex(alvo_ref.id)):
                texto_alvo = normalizar_espacos(ref.content or "")[:900]
                texto_denuncia = normalizar_espacos(remover_mencao_bot(mensagem.content))[:500]

                payload = {
                    "mode": "reply_report",
                    "reporter_id": mensagem.author.id,
                    "target_id": alvo_ref.id,
                    "target_text": texto_alvo,
                    "report_text": texto_denuncia,
                    "about_japex_hint": ("japex" in texto_alvo.lower()) or ("japex" in texto_denuncia.lower()),
                }

                decision = await decidir_punicao_e_motivo(payload)
                act = decision.get("action", "none")
                reason = decision.get("reason", "")

                if act != "none":
                    if act == "ban":
                        ok = await banir(alvo_ref)
                        if ok:
                            await mensagem.channel.send(relatorio_punicao("ban", alvo_ref.display_name, None, reason or "Infração grave."))
                        else:
                            await mensagem.channel.send("Negado.")
                        return

                    dur = duracao_por_action(act)
                    ok = await mutar(alvo_ref, dur)
                    if ok:
                        await mensagem.channel.send(relatorio_punicao(act, alvo_ref.display_name, dur, reason or "Conduta inadequada."))
                    else:
                        await mensagem.channel.send("Negado.")
                    return

        # ---------- 3) Menção direta (sem reply): pode punir o próprio autor se for infração clara ----------
        # (rigidez + justo)
        if not is_japex(mensagem.author.id):
            texto_user = normalizar_espacos(remover_mencao_bot(mensagem.content))[:900]
            payload = {
                "mode": "direct_mention",
                "author_id": mensagem.author.id,
                "author_name": mensagem.author.display_name,
                "text": texto_user,
                "about_japex_hint": ("japex" in texto_user.lower()),
            }

            decision = await decidir_punicao_e_motivo(payload)
            act = decision.get("action", "none")
            reason = decision.get("reason", "")

            if act != "none":
                if act == "ban":
                    ok = await banir(mensagem.author)
                    if ok:
                        await mensagem.channel.send(relatorio_punicao("ban", mensagem.author.display_name, None, reason or "Infração grave."))
                    else:
                        await mensagem.channel.send("Negado.")
                    return

                dur = duracao_por_action(act)
                ok = await mutar(mensagem.author, dur)
                if ok:
                    await mensagem.channel.send(relatorio_punicao(act, mensagem.author.display_name, dur, reason or "Conduta inadequada."))
                else:
                    await mensagem.channel.send("Negado.")
                return

        # ---------- 4) Conversa normal ----------
        texto = remover_mencao_bot(mensagem.content)
        if not texto:
            return

        async with mensagem.channel.typing():
            resposta = await gerar_resposta(texto, mensagem.author, mensagem.channel.id)

        adicionar_historico(mensagem.channel.id, mensagem.author.id, "user", texto)
        adicionar_historico(mensagem.channel.id, mensagem.author.id, "assistant", resposta)

        await mensagem.reply(resposta)

cliente.run(TOKEN_DISCORD)
