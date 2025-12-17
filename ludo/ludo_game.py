# -*- coding: utf-8 -*-
import os
import time
import json
import random
from threading import Lock, Thread

from flask import Blueprint, request, jsonify, session, redirect, url_for, send_from_directory

LUDO_BOT_ID = "LUDO_BOT_JARVIS"
LUDO_TABLES = {}

def _now():
    return time.time()

# Layout clássico (matriz 15x15):
LUDO_GLOBAL_TRACK = [
    (6,1),(6,2),(6,3),(6,4),(6,5),
    (5,6),(4,6),(3,6),(2,6),(1,6),(0,6),
    (0,7),(0,8),
    (1,8),(2,8),(3,8),(4,8),(5,8),
    (6,9),(6,10),(6,11),(6,12),(6,13),(6,14),
    (7,14),(8,14),
    (8,13),(8,12),(8,11),(8,10),(8,9),
    (9,8),(10,8),(11,8),(12,8),(13,8),(14,8),
    (14,7),(14,6),
    (13,6),(12,6),(11,6),(10,6),(9,6),
    (8,5),(8,4),(8,3),(8,2),(8,1),(8,0),
    (7,0),(6,0),
]
LUDO_SAFE_IDX = set([0, 8, 13, 21, 26, 34, 39, 47])

LUDO_START_OFFSET = {
    "green": 0,
    "yellow": 13,
    "blue": 26,
    "red": 39,
}

LUDO_YARD = {
    "green":  [(2,2),(2,3),(3,2),(3,3)],
    "yellow": [(2,11),(2,12),(3,11),(3,12)],
    "red":    [(11,2),(11,3),(12,2),(12,3)],
    "blue":   [(11,11),(11,12),(12,11),(12,12)],
}

LUDO_LANES = {
    "green":  [(7,1),(7,2),(7,3),(7,4),(7,5),(7,6)],
    "yellow": [(1,7),(2,7),(3,7),(4,7),(5,7),(6,7)],
    "blue":   [(7,13),(7,12),(7,11),(7,10),(7,9),(7,8)],
    "red":    [(13,7),(12,7),(11,7),(10,7),(9,7),(8,7)],
}

COLOR_HEX = {
    "green": "#3dff8a",
    "yellow": "#ffd000",
    "blue": "#2f8bff",
    "red": "#ff4d4d",
}

def _turn_uid(t):
    order = t.get("turn_order") or []
    if not order:
        return None
    idx = int(t.get("turn_idx", 0) or 0) % len(order)
    return order[idx]

def _advance_turn(t, keep_same=False):
    if keep_same:
        return
    order = t.get("turn_order") or []
    if not order:
        return
    t["turn_idx"] = (int(t.get("turn_idx", 0) or 0) + 1) % len(order)

def _global_idx(color, step):
    if step is None or step < 0 or step > 50:
        return None
    return (LUDO_START_OFFSET[color] + step) % 52

def _next_step(step, dice):
    if step < 0:
        return 0 if dice == 6 else None
    ns = step + dice
    return ns if ns <= 56 else None

def _build_occupancy(t):
    occ_ring = {}
    occ_lane = set()
    players = t.get("players") or {}
    for uid, p in players.items():
        color = p.get("color")
        pawns = p.get("pawns") or []
        for i, st in enumerate(pawns):
            if st is None:
                continue
            if st >= 51:
                occ_lane.add((str(uid), int(st)))
                continue
            g = _global_idx(color, st)
            if g is None:
                continue
            occ_ring.setdefault(g, []).append((str(uid), i))
    return occ_ring, occ_lane

def _legal_moves(t, uid, dice):
    players = t.get("players") or {}
    me = players.get(uid)
    if not me:
        return []
    color = me.get("color")
    pawns = me.get("pawns") or []
    occ_ring, occ_lane = _build_occupancy(t)

    moves = []
    for i, st in enumerate(pawns):
        ns = _next_step(st, dice)
        if ns is None:
            continue

        if ns >= 51:
            if (str(uid), int(ns)) in occ_lane and st != ns:
                continue
            moves.append(i)
            continue

        g = _global_idx(color, ns)
        here = occ_ring.get(g, [])
        if any(str(pid) == str(uid) for pid, _ in here):
            continue

        opp = [(pid, pi) for pid, pi in here if str(pid) != str(uid)]
        if len(opp) == 0:
            moves.append(i)
            continue

        if len(opp) == 1 and (g not in LUDO_SAFE_IDX):
            moves.append(i)
            continue

    return moves

def _apply_move(t, uid, pawn_idx, dice):
    players = t.get("players") or {}
    me = players.get(uid)
    if not me:
        return False, "Jogador inválido."

    pawns = me.get("pawns") or []
    if pawn_idx < 0 or pawn_idx >= len(pawns):
        return False, "Peça inválida."

    st = pawns[pawn_idx]
    ns = _next_step(st, dice)
    if ns is None:
        return False, "Movimento impossível."

    if pawn_idx not in _legal_moves(t, uid, dice):
        return False, "Movimento bloqueado."

    pawns[pawn_idx] = ns
    me["pawns"] = pawns

    if ns <= 50:
        g = _global_idx(me.get("color"), ns)
        if g is not None and (g not in LUDO_SAFE_IDX):
            occ_ring, _ = _build_occupancy(t)
            here = occ_ring.get(g, [])
            opp = [(pid, pi) for pid, pi in here if str(pid) != str(uid)]
            if len(opp) == 1:
                op_uid, op_pi = opp[0]
                op = players.get(op_uid)
                if op:
                    op_pawns = op.get("pawns") or []
                    if 0 <= op_pi < len(op_pawns):
                        op_pawns[op_pi] = -1
                        op["pawns"] = op_pawns
                        t["last_event"] = {"ts": _now(), "type":"capture", "by":uid, "victim":op_uid}

    if all(x == 56 for x in (me.get("pawns") or [])):
        t["status"] = "finished"
        t["winner_uid"] = uid
        t["last_event"] = {"ts": _now(), "type":"win", "uid": uid}
        return True, "Vitória!"

    return True, "OK"

def _clean_state(t):
    players_list = []
    for uid, p in (t.get("players") or {}).items():
        players_list.append({
            "id": uid,
            "name": p.get("name","Player"),
            "avatar": p.get("avatar",""),
            "color": p.get("color","green"),
            "is_bot": bool(p.get("is_bot")),
            "pawns": p.get("pawns") or [-1,-1,-1,-1],
        })
    players_list.sort(key=lambda x: str(x["id"]))
    return {
        "name": t.get("name"),
        "bet": int(t.get("bet",0) or 0),
        "pot": int(t.get("pot",0) or 0),
        "owner": t.get("owner"),
        "status": t.get("status","waiting"),
        "turn_uid": _turn_uid(t),
        "dice": t.get("dice"),
        "winner_uid": t.get("winner_uid"),
        "players_list": players_list,
        "last_event": t.get("last_event"),
    }

def _payout_if_needed(t, atualizar_saldo):
    if t.get("payout_done"):
        return
    if t.get("status") != "finished":
        return
    winner = t.get("winner_uid")
    pot = int(t.get("pot",0) or 0)
    if not winner or pot <= 0:
        t["payout_done"] = True
        return
    if winner == LUDO_BOT_ID:
        t["payout_done"] = True
        return
    atualizar_saldo(str(winner), pot, "Ludo Win")
    t["payout_done"] = True

def _trigger_bot_if_needed(t, atualizar_saldo):
    if t.get("status") != "playing":
        return
    if _turn_uid(t) != LUDO_BOT_ID:
        return
    if t.get("_bot_busy"):
        return
    t["_bot_busy"] = True

    def run():
        try:
            time.sleep(0.65)
            with t["_lock"]:
                if t.get("status") != "playing":
                    t["_bot_busy"] = False
                    return

                if t.get("dice") is None:
                    t["dice"] = random.randint(1,6)
                dice = int(t.get("dice") or 1)

                legal = _legal_moves(t, LUDO_BOT_ID, dice)
                if not legal:
                    t["last_event"] = {"ts": _now(), "uid": LUDO_BOT_ID, "dice": dice, "pass": True}
                    t["dice"] = None
                    _advance_turn(t, keep_same=False)
                    t["_bot_busy"] = False
                    return

                players = t.get("players") or {}
                bot = players.get(LUDO_BOT_ID)
                bot_color = bot.get("color") if bot else "blue"

                best = None
                best_score = -10**9
                for pi in legal:
                    st = bot["pawns"][pi]
                    ns = _next_step(st, dice)
                    score = 0

                    if st < 0 and ns == 0:
                        score += 50
                    if st >= 0 and ns is not None:
                        score += (ns - st) * 2

                    if ns is not None and ns <= 50:
                        g = _global_idx(bot_color, ns)
                        if g is not None and (g not in LUDO_SAFE_IDX):
                            occ_ring, _ = _build_occupancy(t)
                            here = occ_ring.get(g, [])
                            opp = [(uid,pidx) for uid,pidx in here if str(uid)!=str(LUDO_BOT_ID)]
                            if len(opp) == 1:
                                score += 120

                    if ns is not None:
                        score += ns * 0.6

                    if score > best_score:
                        best_score = score
                        best = pi

                ok, _msg = _apply_move(t, LUDO_BOT_ID, best, dice)
                if not ok:
                    t["dice"] = None
                    _advance_turn(t, keep_same=False)
                    t["_bot_busy"] = False
                    return

                if t.get("status") == "finished":
                    _payout_if_needed(t, atualizar_saldo)
                    t["dice"] = None
                    t["_bot_busy"] = False
                    return

                extra = (dice == 6)
                t["dice"] = None
                _advance_turn(t, keep_same=extra)
                t["_bot_busy"] = False

        except Exception:
            try:
                with t["_lock"]:
                    t["_bot_busy"] = False
            except Exception:
                pass

    Thread(target=run, daemon=True).start()

def register_ludo(app, get_base_html=None, ler_json=None, atualizar_saldo=None):
    if ler_json is None or atualizar_saldo is None:
        raise RuntimeError("register_ludo precisa receber ler_json e atualizar_saldo do seu projeto.")

    ludo_bp = Blueprint("ludo_bp", __name__)

    @ludo_bp.route("/game/ludo/asset/<path:filename>")
    def ludo_asset(filename):
        base_dir = os.path.dirname(__file__)
        if ".." in filename or filename.startswith(("/", "\\")):
            return "bad request", 400
        return send_from_directory(base_dir, filename, conditional=True)


    @ludo_bp.route("/game/ludo")
    def ludo_lobby():
        if not session.get("user"):
            return redirect(url_for("login"))

        html = r"""
<style>
body{background:#0b0b10}
.wrap{max-width:1200px;margin:18px auto;padding:12px;color:#eee;font-family:system-ui}
.card{border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.06);border-radius:18px;padding:16px;box-shadow:0 18px 50px rgba(0,0,0,.45)}
.h{font-weight:1000;font-size:20px;margin-bottom:10px}
.row{display:grid;grid-template-columns:420px 1fr;gap:14px;align-items:start}
@media(max-width:980px){.row{grid-template-columns:1fr}}
.btn{padding:10px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.25);color:#fff;font-weight:900;cursor:pointer}
.btn.primary{background:#2f8bff}
.btn.warn{background:#ffd000;color:#111}
.in{width:100%;padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.25);color:#fff}
.list{display:flex;flex-direction:column;gap:10px;max-height:520px;overflow:auto}
.item{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:12px;border-radius:16px;border:1px solid rgba(255,255,255,.10);background:rgba(0,0,0,.20)}
.pill{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.14);opacity:.9}
.sub{opacity:.82;font-size:12px;margin-top:2px}
</style>

<div class="wrap">
  <div class="row">
    <div class="card">
      <div class="h">🎲 Ludo • Lobby</div>

      <div style="display:flex;gap:10px">
        <input id="name" class="in" value="Mesa-Ludo" />
        <input id="bet" class="in" type="number" value="50" style="max-width:130px"/>
      </div>

      <div style="display:flex;gap:10px;margin-top:10px;flex-wrap:wrap">
        <button class="btn warn" onclick="createT()">Criar mesa</button>
        <button class="btn" onclick="listT()">Atualizar</button>
      </div>

      <hr style="border-color:rgba(255,255,255,.10);margin:14px 0">
      <div style="font-weight:900;margin-bottom:8px">Mesas disponíveis</div>
      <div id="list" class="list"></div>
    </div>

    <div class="card">
      <div class="h">📌 Dica rápida</div>
      <div style="opacity:.85;line-height:1.5">
        Aqui é só o lobby. Quando você <b>entrar</b> em uma mesa, você vai direto para a tela do jogo.
      </div>
      <div style="margin-top:12px;opacity:.75;font-size:13px">
        ✅ 6 tira peça do pátio. ✅ Captura manda o inimigo pro pátio. ✅ Casas seguras não capturam.
      </div>
    </div>
  </div>
</div>
"""

        script = r"""
<script>
const $ = (id)=>document.getElementById(id);

async function listT(){
  const r=await fetch('/api/ludo/list'); const d=await r.json();
  const html=(d.tables||[]).map(t=>{
    const st=(t.status||'waiting').toUpperCase();
    const safeName = String(t.name).replaceAll("'","\\'");
    return `<div class="item">
      <div style="min-width:0">
        <div style="font-weight:1000;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${t.name}</div>
        <div class="sub">${t.players}/${t.max_players} • aposta ${t.bet} • ${st}</div>
      </div>
      <button class="btn primary" onclick="join('${safeName}')">Entrar</button>
    </div>`;
  }).join('');
  $('list').innerHTML=html||`<div style="opacity:.8">Nenhuma mesa.</div>`;
}

async function createT(){
  const name=$('name').value.trim();
  const bet=parseInt($('bet').value||'0',10);
  const r=await fetch('/api/ludo/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,bet})});
  const d=await r.json();
  if(!d.ok) return alert(d.erro||'Erro');
  await join(name);
}

async function join(name){
  const r=await fetch('/api/ludo/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({table:name})});
  const d=await r.json();
  if(!d.ok) return alert(d.erro||'Erro');
  location.href = '/game/ludo/play?table=' + encodeURIComponent(name);
}

listT();
setInterval(listT, 2500);
</script>
"""
        if get_base_html:
            return get_base_html(html, script)
        return "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head><body>"+html+script+"</body></html>"

    @ludo_bp.route("/game/ludo/play")
    def ludo_play():
        if not session.get("user"):
            return redirect(url_for("login"))

        table = (request.args.get("table") or "").strip()
        if not table:
            return redirect("/game/ludo")

        html = r"""
<style>
body{background:#0b0b10; overflow-x: hidden;}
.wrap{width:98%; max-width:1800px; margin:10px auto; padding:8px; color:#eee; font-family:system-ui}
.card{border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.06);border-radius:18px;padding:16px;box-shadow:0 18px 50px rgba(0,0,0,.45)}
.top{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{font-size:13px;padding:6px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.14);opacity:.92;background:rgba(0,0,0,0.3)}
.btn{padding:10px 18px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.25);color:#fff;font-weight:900;cursor:pointer;transition:0.2s}
.btn:hover{transform:scale(1.02); background:rgba(255,255,255,0.1)}
.btn.primary{background:#2f8bff}
.btn.warn{background:#ffd000;color:#111}
.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}

.grid{display:grid;grid-template-columns:1fr 340px;gap:14px;align-items:start}
@media(max-width:1100px){.grid{grid-template-columns:1fr}}

.stage{
    display:flex; flex-direction:column; align-items:center; gap:12px; width:100%;
    --board: min(100%, 85vh); 
}

canvas{
  width:var(--board); height:var(--board);
  max-width:100%; aspect-ratio:1/1;
  display:block; border-radius:28px;
  border:2px solid rgba(255,255,255,.08);
  background:url('/game/ludo/asset/ludo.avif?v=2') center/100% 100% no-repeat;
  box-shadow:0 0 50px rgba(0,0,0,.7);
}

.controlsbar{
  width:var(--board);
  display:flex;gap:12px;align-items:center;justify-content:space-between;
  flex-wrap:wrap;
  background:rgba(0,0,0,0.2); padding:10px; border-radius:16px;
}

.status{
  flex:1 1 auto;
  min-width:200px;
  padding:12px 16px;
  border-radius:12px;
  background:rgba(30,30,40,.9);
  border:1px solid rgba(255,255,255,.15);
  font-size:15px; font-weight:bold;
  text-align:center;
  box-shadow:inset 0 2px 10px rgba(0,0,0,0.5);
}

.diceWrap{display:flex;gap:14px;align-items:center}
.dice{
  width:72px; height:72px; border-radius:18px;
  border:2px solid rgba(255,255,255,.2);
  display:grid; place-items:center;
  font-weight:900; font-size:32px; color:#111;
  background:linear-gradient(135deg, #fff 0%, #ddd 100%);
  box-shadow:0 10px 25px rgba(0,0,0,.5), inset 0 2px 5px rgba(255,255,255,1);
  transition: transform 0.1s;
}
.dice.rolling{
  animation:shake 0.4s infinite;
  background:#ffd000; color:#000;
}
@keyframes shake {
  0% { transform: translate(1px, 1px) rotate(0deg); }
  10% { transform: translate(-1px, -2px) rotate(-1deg); }
  20% { transform: translate(-3px, 0px) rotate(1deg); }
  30% { transform: translate(3px, 2px) rotate(0deg); }
  40% { transform: translate(1px, -1px) rotate(1deg); }
  50% { transform: translate(-1px, 2px) rotate(-1deg); }
  60% { transform: translate(-3px, 1px) rotate(0deg); }
  70% { transform: translate(3px, 1px) rotate(-1deg); }
  80% { transform: translate(-1px, -1px) rotate(1deg); }
  90% { transform: translate(1px, 2px) rotate(0deg); }
  100% { transform: translate(1px, -2px) rotate(-1deg); }
}

.side{display:flex;flex-direction:column;gap:12px}
.small{opacity:.7;font-size:13px;line-height:1.4}
</style>

<div class="wrap">
  <div class="top">
    <div class="pills">
      <span class="pill" id="pMesa">Mesa</span>
      <span class="pill" id="pBet">Aposta</span>
      <span class="pill" id="pPot">Pote</span>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn" onclick="location.href='/game/ludo'">Sair</button>
      <button class="btn" onclick="location.reload()">Atualizar</button>
    </div>
  </div>

  <div class="grid">
    <div class="card stage">
      <canvas id="cv" width="1200" height="1200"></canvas>

      <div class="controlsbar">
        <div class="diceWrap">
          <button class="btn primary" id="rollBtn" onclick="roll()" style="height:50px; font-size:16px">JOGAR DADO</button>
          <div class="dice" id="dice">🎲</div>
        </div>
        <div class="status" id="status">Aguardando...</div>
      </div>
    </div>

    <div class="card side">
      <div style="font-weight:1000;font-size:20px; color:#ffd000">🎮 Controle</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn" onclick="joinBot()">+ Bot CPU</button>
        <button class="btn warn" id="startBtn" onclick="start()">Começar Jogo</button>
      </div>
      <hr style="border-color:rgba(255,255,255,0.1)">
      <div class="small">
        <b>Instruções:</b><br>
        1. Role o dado na sua vez.<br>
        2. Clique na peça brilhante para mover.<br>
        3. Tire <b>6</b> para sair da base.<br>
        4. Capture inimigos para enviá-los de volta.
      </div>
    </div>
  </div>
</div>
"""

        script = r"""
<script>
const L = {
  table:%TABLE%,
  state:null, you:null,
  track:%TRACK%, start:%START%, safe:new Set(%SAFE%),
  yard:%YARD%, lanes:%LANES%, col:%COLHEX%,
  poll:null, diceAnim:null, r:12, _drawn:null, _legal:null
};

const $ = (id)=>document.getElementById(id);

function startDiceAnim(){
  stopDiceAnim();
  const d = $('dice');
  d.classList.add('rolling');
  L.diceAnim = setInterval(()=>{ 
    d.innerText = Math.floor(Math.random()*6)+1; 
  }, 60);
}

function stopDiceAnim(val){
  if(L.diceAnim){ clearInterval(L.diceAnim); L.diceAnim=null; }
  $('dice').classList.remove('rolling');
  if(val !== undefined) $('dice').innerText = val;
}

function gidx(color, step){
  if(step==null||step<0||step>50) return null;
  return (L.start[color]+step)%52;
}

function stepToCoord(color,step,pawn){
  if(step<0) return L.yard[color][pawn];
  if(step<=50) return L.track[(L.start[color]+step)%52];
  return L.lanes[color][step-51];
}

function occupancy(state){
  const occ={}, occLane={};
  (state.players_list||[]).forEach(p=>{
    (p.pawns||[]).forEach((st,i)=>{
      if(st>=51){ occLane[String(p.id)+'|'+String(st)]=true; return; }
      const gi=gidx(p.color,st);
      if(gi===null) return;
      occ[gi]=occ[gi]||[];
      occ[gi].push({uid:p.id,pawn:i});
    });
  });
  return {occ,occLane};
}

function legalMovesForYou(state){
  if(!state || !state.dice) return new Set();
  if(String(state.turn_uid)!==String(L.you)) return new Set();

  const dice=parseInt(state.dice,10);
  const me=(state.players_list||[]).find(p=>String(p.id)===String(L.you));
  if(!me) return new Set();

  const {occ, occLane}=occupancy(state);
  const legal=new Set();

  function nextStep(step,dice){
    if(step<0) return dice===6?0:null;
    const ns=step+dice;
    return ns<=56?ns:null;
  }

  (me.pawns||[]).forEach((st,i)=>{
    const ns=nextStep(st,dice);
    if(ns===null) return;

    if(ns>=51){
      if(occLane[String(L.you)+'|'+String(ns)] && st!==ns) return;
      legal.add(i); return;
    }

    const gi=(L.start[me.color]+ns)%52;
    const here=occ[gi]||[];
    if(here.some(x=>String(x.uid)===String(L.you))) return;

    const opp=here.filter(x=>String(x.uid)!==String(L.you));
    if(opp.length===0){ legal.add(i); return; }
    if(opp.length===1 && !L.safe.has(gi)){ legal.add(i); return; }
  });

  return legal;
}

function draw(state){
  const cv=$('cv'), ctx=cv.getContext('2d');
  const w=cv.width, h=cv.height;
  ctx.clearRect(0,0,w,h);
  const cell = w/15;
  const legal = legalMovesForYou(state);
  const drawn = [];

  ctx.shadowColor = 'rgba(0,0,0,0.5)';
  ctx.shadowBlur = cell * 0.15;
  ctx.shadowOffsetY = cell * 0.08;

  (state.players_list||[]).forEach(p=>{
    const colHex = L.col[p.color]||'#fff';
    (p.pawns||[]).forEach((st,i)=>{
      const [gr,gc] = stepToCoord(p.color,st,i);
      const cx = (gc + 0.5) * cell;
      const cy = (gr + 0.5) * cell;
      
      let offX = 0, offY = 0;
      if(st >= 0 && occupancy(state).occ[gidx(p.color,st)]?.length > 1){
         offX = (i%2 === 0 ? -1 : 1) * (cell*0.15);
         offY = (i < 2 ? -1 : 1) * (cell*0.15);
      }
      const px = cx + offX;
      const py = cy + offY;
      const rr = cell * 0.35;

      if(String(p.id)===String(L.you) && legal.has(i)){
        ctx.beginPath();
        ctx.arc(px, py, rr * 1.3, 0, Math.PI*2);
        ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
        ctx.shadowColor = 'white';
        ctx.shadowBlur = 20;
        ctx.fill();
        ctx.shadowColor = 'rgba(0,0,0,0.5)';
        ctx.shadowBlur = cell * 0.15;
      }

      ctx.beginPath();
      ctx.arc(px, py, rr, 0, Math.PI*2);
      const grad = ctx.createRadialGradient(px - rr*0.3, py - rr*0.3, rr*0.1, px, py, rr);
      grad.addColorStop(0, '#ffffff');
      grad.addColorStop(0.3, colHex);
      grad.addColorStop(1, '#000000');
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.lineWidth = 1.5;
      ctx.strokeStyle = 'rgba(0,0,0,0.4)';
      ctx.stroke();

      drawn.push({uid:p.id, pawn:i, x:px, y:py, r:rr});
    });
  });
  
  ctx.shadowColor = 'transparent';
  ctx.shadowBlur = 0;
  ctx.shadowOffsetY = 0;
  L._drawn=drawn;
  L._legal=legal;
}

$('cv').addEventListener('click', async (ev)=>{
  if(!L.state) return;
  const cv=$('cv');
  const rect=cv.getBoundingClientRect();
  const sx=cv.width/rect.width, sy=cv.height/rect.height;
  const x=(ev.clientX-rect.left)*sx, y=(ev.clientY-rect.top)*sy;

  const turn=L.state.turn_uid, dice=L.state.dice;
  if(String(turn)!==String(L.you) || !dice) return;

  const list = (L._drawn||[]).slice().reverse();
  for(const p of list){
    const d=Math.hypot(p.x-x,p.y-y);
    if(d <= p.r * 1.2){
      if(String(p.uid)!==String(L.you)) continue;
      if(L._legal && L._legal.size && !L._legal.has(p.pawn)) continue;

      const r=await fetch('/api/ludo/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({table:L.table,pawn_idx:p.pawn})});
      const dd=await r.json();
      if(!dd.ok) return alert(dd.erro||'Erro');
      state(); 
      return;
    }
  }
});

async function joinBot(){
  const r=await fetch('/api/ludo/join_bot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({table:L.table})});
  const d=await r.json();
  if(!d.ok) return alert(d.erro||'Erro');
  state();
}

async function start(){
  const r=await fetch('/api/ludo/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({table:L.table})});
  const d=await r.json();
  if(!d.ok) return alert(d.erro||'Erro');
  state();
}

async function roll(){
  if(L.diceAnim) return;
  startDiceAnim();
  const safeTimer = setTimeout(() => stopDiceAnim('?'), 3000);

  try{
    const r=await fetch('/api/ludo/roll',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({table:L.table})
    });
    const d=await r.json();
    clearTimeout(safeTimer);
    if(!d.ok){
      stopDiceAnim('X');
      return alert(d.erro||'Erro');
    }
    setTimeout(async ()=>{ await state(); }, 400);
  }catch(e){
    clearTimeout(safeTimer);
    stopDiceAnim('!');
  }
}

function nameByUid(state, uid){
  const p=(state.players_list||[]).find(x=>String(x.id)===String(uid));
  return p ? (p.name || 'Player') : (String(uid)==='LUDO_BOT_JARVIS'?'🤖 Bot':'Player');
}

async function state(){
  try {
      const r=await fetch('/api/ludo/state?table='+encodeURIComponent(L.table)+'&t='+Date.now());
      const d=await r.json();
      if(!d.ok){ location.href='/game/ludo'; return; }

      L.state=d.table; L.you=d.you_uid;
      $('pMesa').innerText = L.table;
      $('pBet').innerText = 'Aposta: '+(d.table.bet||0);
      $('pPot').innerText = 'Pote: '+(d.table.pot||0);

      if(d.table.dice){
        stopDiceAnim(d.table.dice);
      } else {
        if(!L.diceAnim) $('dice').innerText = '🎲';
      }

      const isOwner=String(d.table.owner)===String(L.you);
      $('startBtn').style.display=(isOwner && d.table.status==='waiting')?'':'none';

      const isMyTurn=(String(d.table.turn_uid)===String(L.you)) && d.table.status==='playing';
      $('rollBtn').disabled = !(isMyTurn && !d.table.dice);
      $('rollBtn').style.opacity = $('rollBtn').disabled ? '0.5' : '1';

      if(d.table.status==='finished'){
        const w=d.table.winner_uid;
        const wName = nameByUid(d.table, w);
        $('status').innerHTML = `<span style="color:#4dff4d">🏆 Vencedor: ${wName}</span>`;
      } else {
        const nm = nameByUid(d.table, d.table.turn_uid);
        const myTurn = (String(d.table.turn_uid)===String(L.you));
        const color = myTurn ? '#ffd000' : '#fff';
        $('status').innerHTML = `Vez de: <span style="color:${color}">${nm}</span>` + (d.table.dice ? ` (Tirou ${d.table.dice})` : '');
      }
      draw(d.table);
  } catch(e){ console.error(e); }
}

state();
if(L.poll) clearInterval(L.poll);
L.poll=setInterval(state, 1000);
</script>
"""
        script = script.replace("%TABLE%", json.dumps(table))
        script = script.replace("%TRACK%", json.dumps(LUDO_GLOBAL_TRACK))
        script = script.replace("%START%", json.dumps(LUDO_START_OFFSET))
        script = script.replace("%SAFE%", json.dumps(list(LUDO_SAFE_IDX)))
        script = script.replace("%YARD%", json.dumps(LUDO_YARD))
        script = script.replace("%LANES%", json.dumps(LUDO_LANES))
        script = script.replace("%COLHEX%", json.dumps(COLOR_HEX))

        if get_base_html:
            return get_base_html(html, script)
        return "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head><body>"+html+script+"</body></html>"

    @ludo_bp.route("/api/ludo/list")
    def api_list():
        tables = []
        for name, t in (LUDO_TABLES or {}).items():
            players = t.get("players") or {}
            tables.append({
                "name": name,
                "bet": int(t.get("bet", 0) or 0),
                "status": t.get("status","waiting"),
                "players": len(players),
                "max_players": 4
            })
        tables.sort(key=lambda x: x["name"])
        return jsonify({"tables": tables})

    @ludo_bp.route("/api/ludo/create", methods=["POST"])
    def api_create():
        user = session.get("user")
        if not user:
            return jsonify({"ok": False, "erro":"Não autenticado"}), 401

        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "erro":"Nome da mesa é obrigatório."}), 400
        if name in LUDO_TABLES:
            return jsonify({"ok": False, "erro":"Já existe uma mesa com esse nome."}), 400

        try:
            bet = int(data.get("bet", 0))
        except Exception:
            bet = 0
        if bet <= 0:
            return jsonify({"ok": False, "erro":"A aposta precisa ser maior que 0."}), 400

        t = {
            "name": name,
            "bet": bet,
            "owner": str(user["id"]),
            "status": "waiting",
            "players": {},
            "turn_order": [],
            "turn_idx": 0,
            "dice": None,
            "winner_uid": None,
            "pot": 0,
            "payout_done": False,
            "created_at": _now(),
            "_lock": Lock(),
            "_bot_busy": False,
            "last_event": None
        }
        LUDO_TABLES[name] = t
        return jsonify({"ok": True})

    @ludo_bp.route("/api/ludo/join", methods=["POST"])
    def api_join():
        user = session.get("user")
        if not user:
            return jsonify({"ok": False, "erro":"Não autenticado"}), 401

        name = (request.json or {}).get("table")
        t = LUDO_TABLES.get(name)
        if not t:
            return jsonify({"ok": False, "erro":"Mesa não encontrada."}), 404

        uid = str(user["id"])
        with t["_lock"]:
            if t.get("status") != "waiting":
                return jsonify({"ok": False, "erro":"Essa mesa já começou."}), 400

            players = t.get("players") or {}
            if uid not in players and len(players) >= 4:
                return jsonify({"ok": False, "erro":"Mesa cheia."}), 400

            used = set([p.get("color") for p in players.values()])
            pref = ["green","yellow","red","blue"]
            color = next((c for c in pref if c not in used), None) or "green"

            players[uid] = {
                "id": uid,
                "name": user.get("username","Player"),
                "avatar": user.get("avatar",""),
                "color": color,
                "is_bot": False,
                "pawns": [-1,-1,-1,-1]
            }
            t["players"] = players
            if uid not in t.get("turn_order", []):
                t["turn_order"].append(uid)

        return jsonify({"ok": True})

    @ludo_bp.route("/api/ludo/join_bot", methods=["POST"])
    def api_join_bot():
        user = session.get("user")
        if not user:
            return jsonify({"ok": False, "erro":"Não autenticado"}), 401

        name = (request.json or {}).get("table")
        t = LUDO_TABLES.get(name)
        if not t:
            return jsonify({"ok": False, "erro":"Mesa não encontrada."}), 404

        with t["_lock"]:
            if t.get("status") != "waiting":
                return jsonify({"ok": False, "erro":"A mesa já começou."}), 400

            players = t.get("players") or {}
            if LUDO_BOT_ID in players:
                return jsonify({"ok": False, "erro":"O bot já está na mesa."}), 400

            if len(players) < 1:
                return jsonify({"ok": False, "erro":"Entre na mesa antes de adicionar o bot."}), 400
            if len(players) >= 4:
                return jsonify({"ok": False, "erro":"Mesa cheia."}), 400

            used = set([p.get("color") for p in players.values()])
            pref = ["yellow","blue","red","green"]
            color = next((c for c in pref if c not in used), None) or "blue"

            players[LUDO_BOT_ID] = {
                "id": LUDO_BOT_ID,
                "name": "Jarvis Bot",
                "avatar": "https://i.imgur.com/6VBx3io.png",
                "color": color,
                "is_bot": True,
                "pawns": [-1,-1,-1,-1]
            }
            t["players"] = players
            if LUDO_BOT_ID not in t.get("turn_order", []):
                t["turn_order"].append(LUDO_BOT_ID)

        return jsonify({"ok": True})

    @ludo_bp.route("/api/ludo/start", methods=["POST"])
    def api_start():
        user = session.get("user")
        if not user:
            return jsonify({"ok": False, "erro":"Não autenticado"}), 401

        name = (request.json or {}).get("table")
        t = LUDO_TABLES.get(name)
        if not t:
            return jsonify({"ok": False, "erro":"Mesa não encontrada."}), 404

        uid = str(user["id"])
        with t["_lock"]:
            if t.get("status") != "waiting":
                return jsonify({"ok": False, "erro":"A partida já iniciou."}), 400
            if str(t.get("owner")) != uid:
                return jsonify({"ok": False, "erro":"Somente o dono da mesa pode iniciar."}), 403

            players = t.get("players") or {}
            humans = [pid for pid, p in players.items() if not p.get("is_bot")]
            if len(humans) < 1:
                return jsonify({"ok": False, "erro":"Entre na mesa antes de iniciar."}), 400

            bet = int(t.get("bet",0) or 0)
            for pid in humans:
                atualizar_saldo(str(pid), -bet, "Ludo Bet")

            t["pot"] = bet * len(humans)
            t["status"] = "playing"
            t["turn_idx"] = 0
            t["dice"] = None
            t["winner_uid"] = None
            t["payout_done"] = False

        return jsonify({"ok": True})

    @ludo_bp.route("/api/ludo/state")
    def api_state():
        user = session.get("user")
        if not user:
            return jsonify({"ok": False, "erro":"Não autenticado"}), 401

        name = request.args.get("table")
        t = LUDO_TABLES.get(name)
        if not t:
            return jsonify({"ok": False, "erro":"Mesa não encontrada."}), 404

        with t["_lock"]:
            _trigger_bot_if_needed(t, atualizar_saldo)
            _payout_if_needed(t, atualizar_saldo)
            state = _clean_state(t)

        return jsonify({"ok": True, "table": state, "you_uid": str(user["id"])})

    @ludo_bp.route("/api/ludo/roll", methods=["POST"])
    def api_roll():
        user = session.get("user")
        if not user:
            return jsonify({"ok": False, "erro":"Não autenticado"}), 401

        name = (request.json or {}).get("table")
        t = LUDO_TABLES.get(name)
        if not t:
            return jsonify({"ok": False, "erro":"Mesa não encontrada."}), 404

        uid = str(user["id"])
        with t["_lock"]:
            if t.get("status") != "playing":
                return jsonify({"ok": False, "erro":"A partida ainda não começou."}), 400
            if _turn_uid(t) != uid:
                return jsonify({"ok": False, "erro":"Não é sua vez."}), 400
            if t.get("dice") is not None:
                return jsonify({"ok": False, "erro":"Você já rolou o dado."}), 400

            t["dice"] = random.randint(1,6)
            dice = int(t["dice"])

            legal = _legal_moves(t, uid, dice)
            if not legal:
                t["last_event"] = {"ts": _now(), "uid": uid, "dice": dice, "pass": True}
                t["dice"] = None
                _advance_turn(t, keep_same=False)

        return jsonify({"ok": True})

    @ludo_bp.route("/api/ludo/move", methods=["POST"])
    def api_move():
        user = session.get("user")
        if not user:
            return jsonify({"ok": False, "erro":"Não autenticado"}), 401

        data = request.json or {}
        name = data.get("table")
        pawn_idx = int(data.get("pawn_idx", -1))

        t = LUDO_TABLES.get(name)
        if not t:
            return jsonify({"ok": False, "erro":"Mesa não encontrada."}), 404

        uid = str(user["id"])
        with t["_lock"]:
            if t.get("status") != "playing":
                return jsonify({"ok": False, "erro":"A partida ainda não começou."}), 400
            if _turn_uid(t) != uid:
                return jsonify({"ok": False, "erro":"Não é sua vez."}), 400

            dice = t.get("dice")
            if dice is None:
                return jsonify({"ok": False, "erro":"Role o dado primeiro."}), 400

            ok, msg = _apply_move(t, uid, pawn_idx, int(dice))
            if not ok:
                return jsonify({"ok": False, "erro": msg}), 400

            if t.get("status") == "finished":
                _payout_if_needed(t, atualizar_saldo)
                t["dice"] = None
                return jsonify({"ok": True})

            extra = (int(dice) == 6)
            t["dice"] = None
            _advance_turn(t, keep_same=extra)
            _trigger_bot_if_needed(t, atualizar_saldo)

        return jsonify({"ok": True})

    app.register_blueprint(ludo_bp)