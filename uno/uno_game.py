# -*- coding: utf-8 -*-
import os
import time
import uuid
import random
import threading
import json
from datetime import datetime, timedelta, timezone
from flask import Blueprint, render_template_string, request, jsonify, session, redirect, send_from_directory, current_app

uno_bp = Blueprint("uno", __name__)

# ----------------------------
# CONFIGURAÇÃO DE SEGURANÇA
# ----------------------------
def configure_secret(app):
    app.secret_key = "UNO_SECRET_KEY_FINAL_V7_LOGIN"

# ----------------------------
# SISTEMA DE ECONOMIA (INTEGRAÇÃO COM main.py)
# ----------------------------
# Procura o arquivo na pasta raiz onde o main.py roda
ARQUIVO_ECONOMIA = os.path.join(os.getcwd(), "economia.json")
ARQUIVO_HISTORICO = os.path.join(os.getcwd(), "historico_transacoes.json")

def get_user_balance(user_id):
    """Lê o saldo direto do arquivo do bot."""
    if not os.path.exists(ARQUIVO_ECONOMIA):
        return 0
    try:
        with open(ARQUIVO_ECONOMIA, "r", encoding="utf-8") as f:
            dados = json.load(f)
        # O ID no json é string
        return dados.get(str(user_id), {}).get("saldo", 0)
    except:
        return 0

def update_user_balance(user_id, amount):
    """Atualiza o saldo e salva o log, igual ao bot."""
    if os.path.exists(ARQUIVO_ECONOMIA):
        try:
            with open(ARQUIVO_ECONOMIA, "r", encoding="utf-8") as f:
                dados = json.load(f)
        except: dados = {}
    else: dados = {}

    uid = str(user_id)
    
    # Cria perfil se não existir (para evitar erros)
    if uid not in dados:
        dados[uid] = {"saldo": 0, "ultimo_daily": "", "nome": f"Player {uid[:4]}"}

    # Atualiza Saldo
    dados[uid]["saldo"] += amount
    
    # Salva Economia
    try:
        with open(ARQUIVO_ECONOMIA, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Erro ao salvar economia UNO: {e}")

    # 2. Salvar Histórico (Log)
    try:
        if os.path.exists(ARQUIVO_HISTORICO):
            with open(ARQUIVO_HISTORICO, "r", encoding="utf-8") as f:
                historico = json.load(f)
        else: historico = []

        agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M:%S")
        
        historico.append({
            "data": agora,
            "uid": uid,
            "nome": dados[uid].get("nome", "Uno Player"),
            "valor": amount,
            "motivo": "UNO Web Game"
        })

        if len(historico) > 5000: historico = historico[-5000:]

        with open(ARQUIVO_HISTORICO, "w", encoding="utf-8") as f:
            json.dump(historico, f, indent=4, ensure_ascii=False)
    except: pass

    return dados[uid]["saldo"]

# ----------------------------
# Memória do Jogo
# ----------------------------
UNO_TABLES = {}
UNO_LOCKS = {}
BOT_ID = "bot_jarvis"
BOT_NAME = "Jarvis"
BOT_AVATAR = "https://cdn-icons-png.flaticon.com/512/4712/4712109.png"

# ----------------------------
# Helpers
# ----------------------------
def _now_ms():
    return int(time.time() * 1000)

def _table_lock(table_id: str) -> threading.Lock:
    if table_id not in UNO_LOCKS:
        UNO_LOCKS[table_id] = threading.Lock()
    return UNO_LOCKS[table_id]

def _user():
    # Se não tiver UID, gera um temporário
    if "uid" not in session:
        session["uid"] = uuid.uuid4().hex[:8]
        session.permanent = True
    if "name" not in session:
        session["name"] = f"Player {str(session['uid'])[:3]}"
    
    return {
        "id": session["uid"],
        "name": session["name"],
        "avatar": session.get("avatar") or "",
    }

# ----------------------------
# Lógica do Baralho
# ----------------------------
COLORS = ["R", "G", "B", "Y"]

def make_deck():
    deck = []
    for c in COLORS:
        deck.append({"c": c, "v": "0"})
    for c in COLORS:
        for n in range(1, 10):
            deck.append({"c": c, "v": str(n)})
            deck.append({"c": c, "v": str(n)})
    for c in COLORS:
        for _ in range(2):
            deck.append({"c": c, "v": "skip"})
            deck.append({"c": c, "v": "rev"})
            deck.append({"c": c, "v": "draw2"})
    
    for _ in range(4):
        deck.append({"c": "W", "v": "wild"})
        deck.append({"c": "W", "v": "wild4"})
    
    # Cartas de Troca (Swap)
    for _ in range(2):
        deck.append({"c": "W", "v": "swap"})

    random.shuffle(deck)
    return deck

def card_img(card):
    c = card["c"]
    v = card["v"]
    if c == "W":
        if v == "wild4": return "W_wild4.png"
        if v == "swap": return "swap.png" 
        return "W_wild.png"
    if v == "rev":
        return f"{c}_reverse.png"
    return f"{c}_{v}.png"

def top_discard(table):
    return table["discard"][-1] if table["discard"] else None

def rebuild_deck_from_discard(table):
    if len(table["discard"]) <= 1:
        return make_deck()
    top = table["discard"][-1]
    rest = table["discard"][:-1]
    random.shuffle(rest)
    table["discard"] = [top]
    return rest

def is_playable(card, table, player_hand):
    top = top_discard(table)
    cur = table.get("current_color")
    if top is None:
        return True
    if card["c"] == "W":
        if card["v"] == "wild4":
            if not cur: return True
            return not any(c2["c"] == cur for c2 in player_hand)
        return True
    if cur and card["c"] == cur: return True
    if card["c"] == top["c"]: return True
    if card["v"] == top["v"]: return True
    return False

def next_index(table, steps=1):
    n = len(table["players"])
    if n == 0: return 0
    idx = table["turn"]
    dir_ = table["direction"]
    return (idx + dir_ * steps) % n

def _set_turn(table, idx):
    table["turn"] = idx
    table["turn_drawn_pid"] = None

def apply_after_play(table, played_card):
    if played_card["c"] == "W":
        if not table.get("current_color"):
            table["current_color"] = random.choice(COLORS)
    else:
        table["current_color"] = played_card["c"]

    v = played_card["v"]
    n_players = len(table["players"])
    draw_n = 0
    skip_next = False

    if v == "rev":
        if n_players == 2: skip_next = True
        else: table["direction"] *= -1
    elif v == "skip": skip_next = True
    elif v == "draw2": draw_n = 2; skip_next = True
    elif v == "wild4": draw_n = 4; skip_next = True
    elif v == "swap":
        me_idx = table["turn"]
        target_idx = next_index(table, 1)
        me = table["players"][me_idx]
        target = table["players"][target_idx]
        me["hand"], target["hand"] = target["hand"], me["hand"]
        table["history"].append({"t": _now_ms(), "msg": f"{me['name']} trocou de mão com {target['name']}!"})

    _set_turn(table, next_index(table, 1))

    if draw_n > 0:
        victim = table["players"][table["turn"]]
        for _ in range(draw_n):
            if not table["deck"]: table["deck"] = rebuild_deck_from_discard(table)
            victim["hand"].append(table["deck"].pop())
        table["history"].append({"t": _now_ms(), "msg": f"{victim['name']} +{draw_n}"})
        if skip_next:
            _set_turn(table, next_index(table, 1))

    if skip_next and draw_n == 0:
        _set_turn(table, next_index(table, 1))

def ensure_started(table):
    if table["status"] != "playing":
        raise ValueError("Jogo não iniciado.")

def table_public_snapshot(table):
    return {
        "id": table["id"],
        "name": table["name"],
        "status": table["status"],
        "max_players": table["max_players"],
        "bet": table.get("bet", 0),
        "players": [{"id": p["id"], "name": p["name"], "avatar": p["avatar"]} for p in table["players"]],
    }

def _ensure_bot_turn(table_id):
    table = UNO_TABLES.get(table_id)
    if not table or table["status"] != "playing": return
    if table.get("bot_pending"): return
    curp = table["players"][table["turn"]]
    if curp["id"] != BOT_ID: return
    table["bot_pending"] = True

    def _bot_worker():
        time.sleep(random.uniform(1.0, 2.5))
        with _table_lock(table_id):
            t = UNO_TABLES.get(table_id)
            if not t or t["status"] != "playing": return
            if t["players"][t["turn"]]["id"] != BOT_ID:
                t["bot_pending"] = False
                return
            bot = t["players"][t["turn"]]
            playable = [(i,c) for i,c in enumerate(bot["hand"]) if is_playable(c,t,bot["hand"])]
            
            if not playable:
                if not t["deck"]: t["deck"] = rebuild_deck_from_discard(t)
                newc = t["deck"].pop()
                bot["hand"].append(newc)
                t["history"].append({"t": _now_ms(), "msg": f"{BOT_NAME} comprou."})
                if is_playable(newc, t, bot["hand"]):
                    playable = [(len(bot["hand"])-1, newc)]
                else:
                    _set_turn(t, next_index(t,1))
                    t["history"].append({"t": _now_ms(), "msg": f"{BOT_NAME} passou."})
                    t["bot_pending"] = False
                    _ensure_bot_turn(table_id)
                    return

            cur_color = t.get("current_color")
            def score(item):
                i,c = item
                s=0
                if c["c"]=="W": s-=5 
                if c["c"]=="W" and c["v"]=="swap" and len(bot["hand"]) > 4: s+=10
                if cur_color and c["c"]==cur_color: s+=2
                if c["v"] in ("draw2","wild4","skip"): s+=3
                return s + random.random()
            
            playable.sort(key=score, reverse=True)
            idx, card = playable[0]

            if card["c"]=="W":
                counts={c:0 for c in COLORS}
                for cc in bot["hand"]:
                    if cc["c"] in COLORS: counts[cc["c"]]+=1
                choose=max(counts.items(), key=lambda x:x[1])[0]
                t["current_color"] = choose

            bot["hand"].pop(idx)
            t["discard"].append(card)
            t["history"].append({"t": _now_ms(), "msg": f"{BOT_NAME} jogou."})

            if len(bot["hand"])==0:
                t["status"]="ended"
                t["winner"]=BOT_ID
                t["history"].append({"t": _now_ms(), "msg": f"{BOT_NAME} venceu!"})
                t["bot_pending"]=False
                return

            apply_after_play(t, card)
            t["bot_pending"]=False
            _ensure_bot_turn(table_id)

    threading.Thread(target=_bot_worker, daemon=True).start()

# ----------------------------
# Table creation
# ----------------------------
def create_table(name, max_players=2, vs_bot=True, bet=0, host=None):
    table_id = uuid.uuid4().hex[:10]
    host = host or _user()
    
    if bet > 0:
        bal = get_user_balance(host["id"])
        if bal < bet:
            raise ValueError("Saldo insuficiente.")
        update_user_balance(host["id"], -bet)

    table = {
        "id": table_id,
        "name": name or f"Mesa de {host['name']}",
        "max_players": max(2, min(int(max_players or 2), 4)),
        "bet": int(bet),
        "pot": int(bet), 
        "status": "waiting",
        "created_at": _now_ms(),
        "host_id": host["id"],
        "players": [],
        "deck": [],
        "discard": [],
        "turn": 0,
        "turn_drawn_pid": None,
        "direction": 1,
        "current_color": None,
        "winner": None,
        "history": [],
        "bot_pending": False,
    }
    table["players"].append({"id": host["id"], "name": host["name"], "avatar": host["avatar"], "hand": []})
    if vs_bot:
        table["players"].append({"id": BOT_ID, "name": BOT_NAME, "avatar": BOT_AVATAR, "hand": []})
    
    UNO_TABLES[table_id] = table
    return table

def start_table(table):
    if len(table["players"]) < 2: raise ValueError("Mínimo 2 jogadores.")
    table["deck"] = make_deck()
    table["discard"] = []
    table["direction"] = 1
    table["current_color"] = None
    table["winner"] = None
    table["history"] = [{"t": _now_ms(), "msg": "Jogo iniciado!"}]
    table["bot_pending"] = False
    table["turn_drawn_pid"] = None

    for p in table["players"]:
        p["hand"] = []
        for _ in range(7): p["hand"].append(table["deck"].pop())

    while True:
        c = table["deck"].pop()
        table["discard"].append(c)
        if not (c["c"]=="W" and c["v"]=="wild4" and c["v"]!="swap"): break
        table["deck"].insert(0,c); random.shuffle(table["deck"])

    table["current_color"] = random.choice(COLORS) if c["c"]=="W" else c["c"]
    table["turn"] = 0
    table["status"] = "playing"

# ----------------------------
# Views & Routes
# ----------------------------
@uno_bp.route('/cartas_uno/<path:filename>')
def uno_cards(filename):
    base = os.path.join(os.path.dirname(__file__), 'cartas_uno')
    return send_from_directory(base, filename)

# ----------------------------
# LOBBY HTML (COM LOGIN E BOTÃO VOLTAR)
# ----------------------------
LOBBY_HTML = r"""<!doctype html>
<html lang='pt-BR'>
<head>
  <meta charset='utf-8'>
  <title>UNO Lobby</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src='https://cdn.tailwindcss.com'></script>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Fredoka', sans-serif; background: #1a202c; color: white; }
    .card-panel { background: white; color: #333; border-radius: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.2); }
    .btn-push { transition: transform 0.1s; border-bottom: 4px solid rgba(0,0,0,0.15); }
    .btn-push:active { transform: translateY(3px); border-bottom: 0; margin-top: 3px; }
  </style>
</head>
<body class="min-h-screen flex items-center justify-center p-4 bg-gradient-to-br from-indigo-900 to-purple-900">
  
  <!-- BOTÃO VOLTAR AO SITE -->
  <div class="absolute top-4 left-4">
      <a href="/" class="bg-white/20 hover:bg-white/30 text-white font-bold py-2 px-4 rounded-full btn-push backdrop-blur-sm">
        🏠 Voltar ao Site
      </a>
  </div>

  <div class="max-w-4xl w-full">
    <div class="flex flex-col items-center mb-8">
       <h1 class="text-6xl font-bold text-yellow-400 drop-shadow-lg tracking-wide mb-2">UNO</h1>
       
       <div class="flex items-center gap-3">
           <div id="myBalance" class="bg-black/40 px-6 py-2 rounded-full text-yellow-300 font-bold border border-yellow-500/30">
              💰 Carregando...
           </div>
           <!-- BOTÃO LOGIN -->
           <button onclick="loginDiscord()" class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-full btn-push text-sm">
              🔗 Conectar Discord
           </button>
       </div>
       <div class="text-xs text-white/50 mt-1">Se seu saldo for 0, clique em Conectar e coloque seu ID.</div>
    </div>

    <div class="grid md:grid-cols-2 gap-6">
      <div class="card-panel p-6">
         <h2 class="text-2xl font-bold mb-4 text-purple-600">Criar Sala</h2>
         
         <label class="text-xs font-bold text-gray-500 uppercase">Nome da Sala</label>
         <input id="tName" class="w-full bg-gray-100 border-2 border-gray-200 rounded-xl px-4 py-3 font-bold text-gray-700 mb-3 outline-none focus:border-purple-400" placeholder="Ex: Sala VIP">
         
         <label class="text-xs font-bold text-gray-500 uppercase">Aposta (Moedas)</label>
         <input type="number" id="tBet" value="0" min="0" class="w-full bg-yellow-50 border-2 border-yellow-200 rounded-xl px-4 py-3 font-bold text-yellow-700 mb-3 outline-none focus:border-yellow-400" placeholder="0 = Grátis">
         
         <div class="flex gap-4 mb-4">
             <div class="flex-1">
                 <label class="text-xs font-bold text-gray-500 uppercase">Jogadores</label>
                 <select id="tMax" class="w-full bg-gray-100 border-2 border-gray-200 rounded-xl px-4 py-2 font-bold text-gray-700 outline-none">
                     <option value="2">2</option><option value="3">3</option><option value="4">4</option>
                 </select>
             </div>
             <div class="flex-1 flex flex-col justify-end">
                 <label class="flex items-center gap-2 cursor-pointer bg-gray-100 px-4 py-2 rounded-xl border-2 border-gray-200 select-none hover:bg-gray-200">
                     <input type="checkbox" id="vsBot" checked class="w-5 h-5 text-purple-600 rounded">
                     <span class="font-bold text-gray-600 text-sm">Incluir Bot</span>
                 </label>
             </div>
         </div>
         
         <button onclick="createTable()" class="w-full bg-green-500 hover:bg-green-600 text-white font-bold py-4 rounded-xl text-xl btn-push shadow-lg shadow-green-500/30">
             CRIAR
         </button>
      </div>

      <div class="card-panel p-6 flex flex-col">
         <div class="flex justify-between items-center mb-4">
             <h2 class="text-2xl font-bold text-blue-600">Salas</h2>
             <button onclick="loadTables()" class="text-sm font-bold text-gray-400 hover:text-blue-500">Atualizar</button>
         </div>
         <div id="tables" class="flex-1 overflow-y-auto space-y-3 max-h-[300px]">
             <div class="text-center text-gray-400 py-10">Carregando...</div>
         </div>
      </div>
    </div>
  </div>

  <script>
    async function api(path, body){
      const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
      return r.json();
    }
    
    function loginDiscord(){
        const id = prompt("Digite seu ID do Discord (Ative modo desenvolvedor para copiar):");
        if(id){
            // Recarrega com o ID na URL para o servidor pegar
            location.href = "/game/uno?uid=" + id;
        }
    }
    
    async function loadTables(){
      const rList = await fetch('/api/uno/list');
      const data = await rList.json();
      
      if(data.balance !== undefined) {
         document.getElementById('myBalance').innerText = '💰 ' + data.balance;
      }

      const el = document.getElementById('tables');
      if(!data.tables.length){ el.innerHTML = "<div class='text-center py-10 text-gray-400'>Nenhuma sala encontrada.</div>"; return; }
      el.innerHTML = data.tables.map(t => {
        const betBadge = t.bet > 0 ? `<span class="bg-yellow-400 text-yellow-900 text-xs px-2 py-1 rounded-full font-bold ml-2">💰 ${t.bet}</span>` : '';
        return `
        <div class="bg-gray-50 border-2 border-gray-100 rounded-xl p-3 flex justify-between items-center">
            <div>
                <div class="font-bold text-gray-800 flex items-center">${t.name} ${betBadge}</div>
                <div class="text-xs font-bold ${t.status==='playing'?'text-red-500':'text-green-500'} uppercase">
                    ${t.status==='playing'?'Em Jogo':'Aguardando'} • ${t.players.length}/${t.max_players}
                </div>
            </div>
            ${t.status==='waiting' ? `<button onclick="join('${t.id}')" class="bg-blue-500 text-white font-bold px-4 py-2 rounded-lg btn-push text-sm">Entrar</button>` : ''}
        </div>
      `}).join('');
    }

    async function createTable(){
        const name = document.getElementById('tName').value;
        const maxp = document.getElementById('tMax').value;
        const vsBot = document.getElementById('vsBot').checked;
        const bet = document.getElementById('tBet').value;
        
        const r = await api('/api/uno/create', {name, max_players:maxp, vs_bot:vsBot, bet:bet});
        if(r.ok) location.href = '/game/uno/'+r.table_id;
        else alert(r.error || "Erro ao criar (Verifique o saldo)");
    }
    async function join(id){
        const r = await api('/api/uno/join', {table_id:id});
        if(r.ok) location.href = '/game/uno/'+id;
        else alert(r.error || "Erro ao entrar (Verifique o saldo)");
    }
    loadTables();
  </script>
</body>
</html>
"""

# ----------------------------
# GAME HTML
# ----------------------------
GAME_HTML = r"""<!doctype html>
<html lang='pt-BR'>
<head>
  <meta charset='utf-8'>
  <title>UNO Match</title>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <script src='https://cdn.tailwindcss.com'></script>
  <link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root { --card-w: 80px; --card-h: 120px; }
    @media(min-width: 768px){ :root{ --card-w: 100px; --card-h: 150px; } }
    
    body {
        font-family: 'Fredoka', sans-serif;
        background: radial-gradient(circle at center, #8B4513 0%, #3E2723 100%);
        overflow: hidden;
        user-select: none;
    }
    .btn-game { border-bottom: 5px solid rgba(0,0,0,0.2); transition: all 0.1s; text-transform: uppercase; letter-spacing: 1px; }
    .btn-game:active { transform: translateY(4px); border-bottom: 0; margin-top: 4px; }
    .btn-game:disabled { opacity: 0.5; filter: grayscale(1); cursor: not-allowed; }
    .uno-card { width: var(--card-w); height: var(--card-h); background: white; border-radius: 12px; position: relative; box-shadow: 2px 5px 15px rgba(0,0,0,0.4); border: 5px solid white; transition: transform 0.2s cubic-bezier(0.25, 0.8, 0.25, 1); pointer-events: none; }
    .uno-card img { width: 100%; height: 100%; object-fit: contain; border-radius: 6px; }
    .swap-badge { position: absolute; bottom: 5px; left: 0; right: 0; text-align: center; background: #9333ea; color: white; font-size: 10px; font-weight: bold; padding: 2px 0; }
    .hand-card-wrap { width: var(--card-w); height: var(--card-h); margin-right: -45px; position: relative; cursor: pointer; }
    .hand-card-wrap:hover .uno-card { transform: translateY(-40px) scale(1.1); z-index: 100 !important; }
    .hand-card-wrap.playable .uno-card { box-shadow: 0 0 0 4px #4ade80, 0 10px 20px rgba(0,0,0,0.5); }
    .avatar-pill { background: white; padding: 5px 15px 5px 5px; border-radius: 99px; display: flex; align-items: center; gap: 10px; box-shadow: 0 5px 15px rgba(0,0,0,0.3); font-weight: bold; color: #333; transition: transform 0.3s; min-width: 140px; position: relative; z-index: 20; }
    .avatar-pill.active { transform: scale(1.1); box-shadow: 0 0 0 4px #fbbf24; }
    .avatar-img { width: 40px; height: 40px; border-radius: 50%; background: #eee; }
    .pile-card { position: absolute; top:0; left:0; transition: transform 0.3s; }
    .pot-display { position: absolute; top: 10px; right: 0; background: #f59e0b; color: #451a03; font-weight: bold; padding: 4px 10px; border-radius: 20px 0 0 20px; box-shadow: -2px 2px 5px rgba(0,0,0,0.3); z-index: 5; }
  </style>
</head>
<body class="h-screen w-screen flex flex-col relative">
  <div class="absolute top-4 left-4 z-50">
      <a href="/game/uno" class="bg-red-500 text-white font-bold px-4 py-2 rounded-xl btn-game shadow-lg text-xs">Sair</a>
  </div>
  <div class="absolute top-4 right-4 z-50 text-right text-white/80">
      <div class="font-bold text-xl" id="tableName">UNO</div>
      <div class="text-xs opacity-60 font-mono" id="roomId">---</div>
  </div>
  <div class="flex-1 flex flex-col p-4 relative">
      <div class="h-[120px] flex justify-center items-center"><div id="topOpp"></div></div>
      <div class="flex-1 flex items-center">
          <div class="w-[80px] md:w-[150px] flex justify-center"><div id="leftOpp" class="transform -rotate-90 md:rotate-0"></div></div>
          <div class="flex-1 flex flex-col items-center justify-center relative">
              <div id="potContainer" class="pot-display hidden">💰 Pote: 0</div>
              <div id="colorRing" class="absolute w-[240px] h-[240px] rounded-full border-[10px] border-white/5 transition-colors duration-500"></div>
              <div class="flex gap-6 z-10">
                  <div onclick="drawCard()" class="uno-card bg-black cursor-pointer hover:brightness-110 active:scale-95 transition pointer-events-auto">
                      <img src="/cartas_uno/back.png">
                      <div class="absolute inset-0 flex items-center justify-center"><span class="text-white font-bold text-sm bg-black/50 px-2 py-1 rounded">UNO</span></div>
                  </div>
                  <div class="relative w-[var(--card-w)] h-[var(--card-h)]">
                      <div id="discardPile"><div class="uno-card opacity-20 border-dashed border-4 border-white/50 bg-transparent"></div></div>
                  </div>
              </div>
              <div id="centerUI" class="absolute -bottom-20 z-50 flex gap-3"></div>
              <div id="statusMsg" class="absolute -top-12 bg-black/30 px-6 py-2 rounded-full text-white font-bold backdrop-blur-sm">Carregando...</div>
          </div>
          <div class="w-[80px] md:w-[150px] flex justify-center"><div id="rightOpp" class="transform rotate-90 md:rotate-0"></div></div>
      </div>
      <div class="h-[150px] w-full flex justify-center items-end pb-4 overflow-visible z-40">
          <div id="myHand" class="flex justify-center" style="min-width: 100px"></div>
      </div>
  </div>
  <div id="colorModal" class="fixed inset-0 bg-black/80 z-[100] hidden flex items-center justify-center backdrop-blur-sm">
      <div class="bg-white p-6 rounded-3xl shadow-2xl text-center">
          <h2 class="text-2xl font-bold mb-4 text-gray-800">Escolha a Cor!</h2>
          <div class="grid grid-cols-2 gap-4">
              <div onclick="pickColor('R')" class="w-20 h-20 bg-red-500 rounded-2xl cursor-pointer hover:scale-105 border-4 border-transparent hover:border-white shadow-lg"></div>
              <div onclick="pickColor('G')" class="w-20 h-20 bg-green-500 rounded-2xl cursor-pointer hover:scale-105 border-4 border-transparent hover:border-white shadow-lg"></div>
              <div onclick="pickColor('B')" class="w-20 h-20 bg-blue-500 rounded-2xl cursor-pointer hover:scale-105 border-4 border-transparent hover:border-white shadow-lg"></div>
              <div onclick="pickColor('Y')" class="w-20 h-20 bg-yellow-400 rounded-2xl cursor-pointer hover:scale-105 border-4 border-transparent hover:border-white shadow-lg"></div>
          </div>
      </div>
  </div>
  <script>
    const TABLE_ID = '__TABLE_ID__';
    let busy=false, pendingWild=null, lastHandCount=0, discardHistory=[];
    const audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    function playSfx(type){
        if(audioCtx.state === 'suspended') audioCtx.resume();
        const osc = audioCtx.createOscillator(); const gain = audioCtx.createGain();
        osc.connect(gain); gain.connect(audioCtx.destination);
        if(type==='turn'){ osc.frequency.value=600; gain.gain.value=0.1; osc.start(); osc.stop(audioCtx.currentTime+0.1); }
        if(type==='play'){ osc.frequency.value=400; gain.gain.value=0.1; osc.start(); osc.stop(audioCtx.currentTime+0.05); }
    }
    async function post(url, data={}){ try{ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); return await r.json(); }catch(e){ return {ok:false}; } }
    
    function renderOpp(elId, p, isTurn){
        const el = document.getElementById(elId);
        if(!p) { el.innerHTML = ''; return; }
        const maxCards=7; const showCount=Math.min(p.count, maxCards);
        let cardsHtml='';
        if(p.count>0) {
            cardsHtml=`<div class="relative h-[30px] w-[60px] mt-2 flex justify-center">`;
            for(let i=0; i<showCount; i++){
                const rot=(i-(showCount-1)/2)*8; const x=(i-(showCount-1)/2)*6;
                cardsHtml+=`<img src="/cartas_uno/back.png" style="position:absolute; width:24px; top:0; transform: translateX(${x}px) rotate(${rot}deg); box-shadow: 1px 1px 3px rgba(0,0,0,0.5); border-radius:2px;">`;
            }
            if(p.count>maxCards) cardsHtml+=`<span class="absolute -right-4 -top-2 bg-red-500 text-white text-[10px] px-1 rounded-full font-bold">+${p.count}</span>`;
            cardsHtml+=`</div>`;
        }
        el.innerHTML = `<div class="flex flex-col items-center"><div class="avatar-pill ${isTurn?'active':''}"><img src="${p.avatar}" class="avatar-img"><div class="flex flex-col leading-tight"><span class="text-xs text-gray-500 uppercase">Opponent</span><span class="text-sm truncate w-[80px]">${p.name}</span></div></div>${cardsHtml}</div>`;
    }
    function renderDiscard(img, color){
        if(!img) return;
        const pile = document.getElementById('discardPile');
        const last = discardHistory[discardHistory.length-1];
        if(!last || last.img !== img){
            if(discardHistory.length>4) discardHistory.shift();
            discardHistory.push({ img: img, rot: (Math.random()*30)-15, x: (Math.random()*10)-5, y: (Math.random()*10)-5 });
            if(last) playSfx('play');
        }
        pile.innerHTML = discardHistory.map((c, i) => `<div class="uno-card pile-card" style="transform: translate(${c.x}px, ${c.y}px) rotate(${c.rot}deg); z-index:${i}"><img src="/cartas_uno/${c.img}"></div>`).join('');
        const colors = {'R':'#ef4444','G':'#22c55e','B':'#3b82f6','Y':'#facc15'};
        document.getElementById('colorRing').style.borderColor = colors[color] || 'rgba(255,255,255,0.1)';
    }
    async function refresh(){
        const data = await post(`/api/uno/state?table_id=${TABLE_ID}`);
        if(!data.ok){ if(data.error === 'Spectator'){ await post('/api/uno/join', {table_id: TABLE_ID}); return; } window.location.href = '/game/uno'; return; }
        document.getElementById('tableName').innerText = data.table_name;
        document.getElementById('roomId').innerText = "#" + TABLE_ID.substring(0,4);
        document.getElementById('statusMsg').innerText = data.status_line;
        if(data.pot>0){ const p=document.getElementById('potContainer'); p.innerText='💰 Prêmio: '+data.pot; p.classList.remove('hidden'); }
        const tName = data.turn_name;
        renderOpp('topOpp', data.opponents.top, data.opponents.top?.name===tName);
        renderOpp('leftOpp', data.opponents.left, data.opponents.left?.name===tName);
        renderOpp('rightOpp', data.opponents.right, data.opponents.right?.name===tName);
        renderDiscard(data.discard_img, data.current_color);
        document.getElementById('myHand').innerHTML = data.my_hand.map((c, i) => {
            const p = c.playable && data.is_my_turn;
            let ex = c.v==='swap'?'<div class="swap-badge">TROCA</div>':'';
            return `<div class="hand-card-wrap ${p?'playable':''}" style="z-index:${i}" onclick="playCard(${i})"><div class="uno-card ${p?'':'brightness-75'}"><img src="/cartas_uno/${c.img}">${ex}</div></div>`;
        }).join('');
        if(data.is_my_turn && lastHandCount!==-1 && !document.hidden) playSfx('turn');
        lastHandCount = data.my_count;
        const ui = document.getElementById('centerUI');
        let btns = '';
        if(data.status==='waiting'){
            if(data.is_host) btns=`<button onclick="startGame()" class="bg-green-500 hover:bg-green-600 text-white text-xl px-10 py-4 rounded-2xl shadow-xl btn-game animate-bounce">INICIAR JOGO</button>`;
            else btns=`<span class="text-white font-bold bg-black/40 px-4 py-2 rounded-full">Aguardando Host...</span>`;
        }else if(data.status==='playing'){
            if(data.is_my_turn){
                btns+=`<button onclick="drawCard()" class="bg-blue-500 text-white px-6 py-3 rounded-xl btn-game shadow-lg ${!data.can_draw?'opacity-50 cursor-not-allowed':''}">Comprar</button>`;
                btns+=`<button onclick="passTurn()" class="bg-gray-500 text-white px-6 py-3 rounded-xl btn-game shadow-lg ${!data.can_pass?'opacity-50 cursor-not-allowed':''}">Pular</button>`;
            }
        }else if(data.is_host) btns=`<button onclick="startGame()" class="bg-purple-500 text-white text-xl px-10 py-4 rounded-2xl btn-game">JOGAR NOVAMENTE</button>`;
        ui.innerHTML = btns;
    }
    async function startGame(){ const b=document.querySelector('#centerUI button'); if(b&&b.innerText.includes("NOVAMENTE")) await post('/api/uno/restart',{table_id:TABLE_ID}); else await post('/api/uno/start',{table_id:TABLE_ID}); refresh(); }
    async function playCard(i){ if(busy)return; const el=document.querySelectorAll('.hand-card-wrap')[i]; if(!el.classList.contains('playable'))return; const src=el.querySelector('img').src; if(src.includes('W_wild')||src.includes('wild4')||src.includes('swap')){ pendingWild=i; document.getElementById('colorModal').classList.remove('hidden'); return; } busy=true; await post('/api/uno/play',{table_id:TABLE_ID, hand_index:i}); busy=false; refresh(); }
    async function pickColor(c){ document.getElementById('colorModal').classList.add('hidden'); if(pendingWild===null)return; busy=true; await post('/api/uno/play',{table_id:TABLE_ID, hand_index:pendingWild, choose_color:c}); pendingWild=null; busy=false; refresh(); }
    async function drawCard(){ if(busy)return; busy=true; await post('/api/uno/draw',{table_id:TABLE_ID}); busy=false; refresh(); }
    async function passTurn(){ if(busy)return; busy=true; await post('/api/uno/pass',{table_id:TABLE_ID}); busy=false; refresh(); }
    setInterval(refresh, 1000); refresh();
  </script>
</body>
</html>
"""

@uno_bp.route('/game/uno/<table_id>')
def uno_game(table_id):
    if table_id not in UNO_TABLES:
        return redirect('/game/uno')
    return render_template_string(GAME_HTML.replace('__TABLE_ID__', table_id))

@uno_bp.route('/game/uno')
def uno_lobby():
    # Se passou UID na URL, loga o usuário
    if request.args.get('uid'):
        uid = request.args.get('uid')
        session['uid'] = uid
        # Tenta pegar o nome se existir no banco
        if os.path.exists(ARQUIVO_ECONOMIA):
            try:
                with open(ARQUIVO_ECONOMIA, "r", encoding="utf-8") as f:
                    dados = json.load(f)
                if uid in dados:
                    session['name'] = dados[uid].get('nome', 'Player')
            except: pass
        return redirect('/game/uno')

    return render_template_string(LOBBY_HTML)

# ----------------------------
# API Endpoints
# ----------------------------
@uno_bp.route('/api/uno/list', methods=['GET','POST'])
def api_list():
    now=_now_ms()
    me=_user()
    tables=[]
    for t in UNO_TABLES.values():
        if t['status']=='ended' and (now-t['created_at'])>30*60*1000: continue
        tables.append(t)
    tables.sort(key=lambda x:x['created_at'], reverse=True)
    return jsonify({
        'ok':True,
        'tables':[table_public_snapshot(t) for t in tables],
        'balance': get_user_balance(me["id"])
    })

@uno_bp.route('/api/uno/create', methods=['POST'])
def api_create():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    name=(data.get('name') or '').strip()
    maxp=int(data.get('max_players') or 2)
    vs_bot=bool(data.get('vs_bot', True))
    bet=int(data.get('bet') or 0)
    try:
        t=create_table(name, max_players=maxp, vs_bot=vs_bot, bet=bet, host=me)
        return jsonify({'ok':True,'table_id':t['id']})
    except ValueError as e:
        return jsonify({'ok':False,'error':str(e)})

@uno_bp.route('/api/uno/join', methods=['POST'])
def api_join():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    tid=data.get('table_id')
    if tid not in UNO_TABLES: return jsonify({'ok':False})
    with _table_lock(tid):
        t=UNO_TABLES[tid]
        if any(p['id']==me['id'] for p in t['players']): return jsonify({'ok':True})
        if len(t['players'])>=t['max_players']: return jsonify({'ok':False, 'error':'Mesa cheia'})
        bet = t.get("bet", 0)
        if bet > 0:
            bal = get_user_balance(me["id"])
            if bal < bet: return jsonify({'ok':False, 'error':'Saldo insuficiente'})
            update_user_balance(me["id"], -bet)
            t["pot"] += bet
        t['players'].append({'id':me['id'],'name':me['name'],'avatar':me['avatar'],'hand':[]})
    return jsonify({'ok':True})

@uno_bp.route('/api/uno/leave', methods=['POST'])
def api_leave():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    tid=data.get('table_id')
    if tid not in UNO_TABLES: return jsonify({'ok':True})
    with _table_lock(tid):
        t=UNO_TABLES[tid]
        if t["status"] == "waiting":
            bet = t.get("bet", 0)
            if bet > 0:
                update_user_balance(me["id"], bet)
                t["pot"] -= bet
        t['players']=[p for p in t['players'] if p['id']!=me['id']]
        if not t['players'] or (len(t['players'])==1 and t['players'][0]['id']==BOT_ID):
            UNO_TABLES.pop(tid, None)
            UNO_LOCKS.pop(tid, None)
        elif t['host_id']==me['id']:
            t['host_id']=t['players'][0]['id']
    return jsonify({'ok':True})

@uno_bp.route('/api/uno/start', methods=['POST'])
def api_start():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    tid=data.get('table_id')
    if tid not in UNO_TABLES: return jsonify({'ok':False})
    with _table_lock(tid):
        t=UNO_TABLES[tid]
        if t['host_id']!=me['id']: return jsonify({'ok':False,'error':'Host only'})
        start_table(t)
    _ensure_bot_turn(tid)
    return jsonify({'ok':True})

@uno_bp.route('/api/uno/state', methods=['GET', 'POST'])
def api_state():
    me=_user(); tid=request.args.get('table_id')
    if not tid or tid not in UNO_TABLES: return jsonify({'ok':False})
    with _table_lock(tid):
        t=UNO_TABLES[tid]
        me_p=None
        for p in t['players']:
            if p['id']==me['id']: me_p=p; break
        if not me_p: return jsonify({'ok':False, 'error':'Spectator'})
        is_my_turn = (t['status']=='playing' and t['players'][t['turn']]['id']==me['id'])
        my_hand=[]
        for c in me_p['hand']:
            item = {'c':c['c'], 'v':c['v'], 'img':card_img(c)}
            item['playable'] = (is_my_turn and is_playable(c,t,me_p['hand']))
            my_hand.append(item)
        others=[p for p in t['players'] if p['id']!=me['id']]
        opp={'top':None,'left':None,'right':None}
        if len(others)==1: opp['top']={'name':others[0]['name'],'avatar':others[0]['avatar'],'count':len(others[0]['hand'])}
        elif len(others)==2:
            opp['left']={'name':others[0]['name'],'avatar':others[0]['avatar'],'count':len(others[0]['hand'])}
            opp['right']={'name':others[1]['name'],'avatar':others[1]['avatar'],'count':len(others[1]['hand'])}
        elif len(others)>=3:
            opp['left']={'name':others[0]['name'],'avatar':others[0]['avatar'],'count':len(others[0]['hand'])}
            opp['top']={'name':others[1]['name'],'avatar':others[1]['avatar'],'count':len(others[1]['hand'])}
            opp['right']={'name':others[2]['name'],'avatar':others[2]['avatar'],'count':len(others[2]['hand'])}
        turn_p = t['players'][t['turn']] if t['players'] else None
        data = {
            'ok':True, 'table_name':t['name'], 'status':t['status'],
            'status_line': f"VEZ DE {turn_p['name'].upper()}" if t['status']=='playing' else ("FIM DE JOGO" if t['status']=='ended' else "AGUARDANDO..."),
            'me_id': me['id'], 'is_host': (t['host_id']==me['id']), 'is_my_turn': is_my_turn,
            'turn_name': turn_p['name'] if turn_p else '',
            'can_draw': (is_my_turn and t['turn_drawn_pid']!=me['id']),
            'can_pass': (is_my_turn and t['turn_drawn_pid']==me['id']),
            'current_color': t['current_color'], 'discard_img': card_img(t['discard'][-1]) if t['discard'] else None,
            'my_hand': my_hand, 'my_count': len(me_p['hand']), 'winner_id': t.get('winner'),
            'opponents': opp, 'pot': t.get('pot', 0)
        }
    _ensure_bot_turn(tid)
    return jsonify(data)

@uno_bp.route('/api/uno/draw', methods=['POST'])
def api_draw():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    tid=data.get('table_id')
    with _table_lock(tid):
        t=UNO_TABLES[tid]; ensure_started(t)
        if t['players'][t['turn']]['id']!=me['id']: return jsonify({'ok':False})
        if t.get('turn_drawn_pid')==me['id']: return jsonify({'ok':False})
        if not t['deck']: t['deck']=rebuild_deck_from_discard(t)
        t['players'][t['turn']]['hand'].append(t['deck'].pop())
        t['turn_drawn_pid']=me['id']
        t['history'].append({'t':_now_ms(),'msg':f"{me['name']} comprou."})
    return jsonify({'ok':True})

@uno_bp.route('/api/uno/pass', methods=['POST'])
def api_pass():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    tid=data.get('table_id')
    with _table_lock(tid):
        t=UNO_TABLES[tid]; ensure_started(t)
        if t['players'][t['turn']]['id']!=me['id']: return jsonify({'ok':False})
        if t.get('turn_drawn_pid')!=me['id']: return jsonify({'ok':False})
        _set_turn(t, next_index(t,1))
    _ensure_bot_turn(tid)
    return jsonify({'ok':True})

@uno_bp.route('/api/uno/play', methods=['POST'])
def api_play():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    tid=data.get('table_id'); idx=data.get('hand_index'); choose=data.get('choose_color')
    with _table_lock(tid):
        t=UNO_TABLES[tid]; ensure_started(t)
        p = t['players'][t['turn']]
        if p['id']!=me['id']: return jsonify({'ok':False})
        card = p['hand'][idx]
        if not is_playable(card,t,p['hand']): return jsonify({'ok':False})
        if card['c']=='W':
            if choose not in COLORS: return jsonify({'ok':False})
            t['current_color']=choose
        p['hand'].pop(idx)
        t['discard'].append(card)
        t['history'].append({'t':_now_ms(),'msg':f"{me['name']} jogou."})
        if not p['hand']:
            t['status']='ended'; t['winner']=me['id']
            if me['id'] != BOT_ID and t.get('pot', 0) > 0:
                update_user_balance(me['id'], t['pot'])
                t['history'].append({'t':_now_ms(),'msg':f"{me['name']} ganhou {t['pot']} moedas!"})
            return jsonify({'ok':True})
        apply_after_play(t, card)
    _ensure_bot_turn(tid)
    return jsonify({'ok':True})

@uno_bp.route('/api/uno/restart', methods=['POST'])
def api_restart():
    me=_user(); data=request.get_json(force=True,silent=True) or {}
    tid=data.get('table_id')
    with _table_lock(tid):
        t=UNO_TABLES[tid]
        if t['host_id']!=me['id']: return jsonify({'ok':False})
        t["pot"] = 0 
        start_table(t)
    _ensure_bot_turn(tid)
    return jsonify({'ok':True})

def register_uno(app):
    configure_secret(app)
    if 'uno' not in app.blueprints:
        app.register_blueprint(uno_bp)
    return app