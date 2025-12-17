# ==============================================================================
# ♣️ TRUCO - módulo extraído para pasta /truco
# - Este arquivo contém TODAS as rotas e comandos do Truco (Blueprint)
# - Dependências do app principal são injetadas via init_truco()
# ==============================================================================

from flask import Blueprint, request, jsonify, session, redirect, url_for
import random
import time
import os
import json
from threading import Lock, Thread
from copy import deepcopy
from collections import Counter

_DEPS = {
    "get_base_html": None,
    "obter_saldo_atual": None,
    "atualizar_saldo": None,
}

def init_truco(get_base_html, obter_saldo_atual, atualizar_saldo, feed_add=None):
    """Chame isso no app principal ANTES de registrar o blueprint."""
    _DEPS["get_base_html"] = get_base_html
    _DEPS["obter_saldo_atual"] = obter_saldo_atual
    _DEPS["atualizar_saldo"] = atualizar_saldo
    _DEPS["feed_add"] = feed_add

def _get_base_html(conteudo, script_extra=""):
    fn = _DEPS.get("get_base_html")
    if fn:
        return fn(conteudo, script_extra)
    # Fallback (evita quebrar em caso de import isolado)
    return str(conteudo) + (str(script_extra) if script_extra else "")

def _obter_saldo_atual(user_id):
    fn = _DEPS.get("obter_saldo_atual")
    if fn:
        return fn(user_id)
    return 0

def _atualizar_saldo(user_id, valor, motivo="Truco"):
    fn = _DEPS.get("atualizar_saldo")
    ok = False
    if fn:
        ok = bool(fn(user_id, valor, motivo))
    # balão no feed (direita) quando alguém ganha/perde
    try:
        feed_fn = _DEPS.get("feed_add")
        if feed_fn and valor and int(valor) != 0:
            kind = "win" if int(valor) > 0 else "loss"
            txt = f"Truco: {motivo}"
            feed_fn(kind, txt, uid=user_id, amount=int(valor))
    except Exception:
        pass
    return ok


# ========================================================================
# ♣️ TRUCO - MÓDULO ÚNICO (ESTILO B - MESA VERDE ESCURA) - VERSÃO FINAL
# - Arquivo pronto para substituir seu módulo/truco atual (single-file)
# - Sem f-strings em blocos JS/HTML (usa .replace())
# - Corrige travamentos do bot, garante próxima jogada corretamente,
#   centraliza cartas, centraliza botões, mostra jogadores já nas posições
# - IA melhorada e proteção contra threads concorrentes
# ========================================================================

truco_bp = Blueprint('truco', __name__)

# -------------------------
# CONFIGURAÇÕES GLOBAIS
# -------------------------
TRUCO_TABLES = {}
BOT_ID = "bot_jarvis"

# -------------------------
# TRUCO - ECONOMIA (aposta / pagamento)
# -------------------------

def _truco_is_real_uid(uid):
    try:
        return str(uid).isdigit()
    except Exception:
        return False

def _truco_collect_buyin(t):
    """Debita a aposta (bet) dos jogadores humanos apenas uma vez por partida."""
    if t.get('_buyin_collected'):
        return True, None

    bet = int(t.get('bet') or 0)
    if bet <= 0:
        t['_buyin_collected'] = True
        t['_paid_out'] = False
        t['_match_players'] = list((t.get('players') or {}).keys())
        return True, None

    players = list((t.get('players') or {}).keys())

    # checa saldo antes (não inicia se alguém não tiver)
    for pid in players:
        if pid == BOT_ID:
            continue
        if not _truco_is_real_uid(pid):
            continue
        try:
            saldo = _obter_saldo_atual(pid)
        except Exception:
            saldo = 0
        if saldo < bet:
            return False, pid

    # debita
    for pid in players:
        if pid == BOT_ID:
            continue
        if not _truco_is_real_uid(pid):
            continue
        try:
            _atualizar_saldo(pid, -bet, 'Truco entrada')
        except Exception:
            pass

    t['_buyin_collected'] = True
    t['_paid_out'] = False
    t['_match_players'] = players
    return True, None

def _truco_payout(t, winner_team=None):
    """Paga o pote ao time vencedor (somente humanos).

    Observação: O pote considera TODOS os participantes na mesa (incluindo bot),
    mas o crédito vai apenas para usuários humanos.
    """
    if t.get('_paid_out'):
        return

    bet = int(t.get('bet') or 0)
    if bet <= 0:
        t['_paid_out'] = True
        return

    participants = t.get('_match_players') or list((t.get('players') or {}).keys())
    participants = [p for p in participants if p in (t.get('players') or {})]
    if not participants:
        t['_paid_out'] = True
        return

    teams = t.get('teams') or {}

    # define time vencedor se não veio
    if not winner_team:
        try:
            ptsA, ptsB = _truco_team_points(t)
        except Exception:
            ptsA, ptsB = 0, 0

        if ptsA >= 12 and ptsB < 12:
            winner_team = 'A'
        elif ptsB >= 12 and ptsA < 12:
            winner_team = 'B'
        else:
            winner_team = 'A' if ptsA >= ptsB else 'B'

    pot = bet * len(participants)

    winners = [pid for pid in participants if teams.get(pid) == winner_team and _truco_is_real_uid(pid)]
    if not winners:
        t['_paid_out'] = True
        return

    share = pot // len(winners)
    rem = pot - (share * len(winners))

    for i, pid in enumerate(winners):
        amt = share + (1 if i < rem else 0)
        try:
            _atualizar_saldo(pid, int(amt), 'Truco prêmio')
        except Exception:
            pass

    t['_paid_out'] = True

def _truco_reset_match_state(t):
    # mantém mesa/jogadores, mas zera a partida
    t['status'] = 'waiting'
    t['deck'] = []
    t['vira'] = None
    t['table_cards'] = []
    t['turn'] = None
    t['valor_mao'] = 1
    t['rodada_atual'] = 1
    t['truco_request'] = None
    t['last_winner_data'] = None
    t['round_history'] = []
    t['team_round_wins'] = {'A': 0, 'B': 0}
    t['team_points'] = {'A': 0, 'B': 0}
    t['truco_disabled'] = False
    t['truco_can_raise_team'] = None
    t['truco_last_raise_team'] = None
    t['mao11'] = {'active': False}
    t['played_cards_global'] = []
    t['_ia_busy'] = False

    # placar por time
    t['team_points'] = {'A': 0, 'B': 0}

    # controle de truco
    t['truco_disabled'] = False
    t['truco_can_raise_team'] = None
    t['truco_last_raise_team'] = None

    # mão de 11 (estado)
    t['mao11'] = {'active': False}

    # reseta flags de apostas/pagamento para permitir nova rodada com moedas
    t['_buyin_collected'] = False
    t['_paid_out'] = False

    for uid, p in (t.get('players', {}) or {}).items():
        p['hand'] = []
        p['round_wins'] = 0
        p['points'] = 0

def _truco_schedule_auto_restart(t, delay=6.0):
    # evita agendar múltiplas vezes
    if t.get('_auto_restart_scheduled'):
        return
    t['_auto_restart_scheduled'] = True

    def _runner():
        try:
            time.sleep(max(0.5, float(delay)))
        except Exception:
            time.sleep(6.0)

        # só reinicia se ainda estiver em finished
        if t.get('status') != 'finished':
            t['_auto_restart_scheduled'] = False
            return

        _truco_reset_match_state(t)

        # compra entrada da próxima partida (se mesa tem aposta)
        ok, who = _truco_collect_buyin(t)
        if not ok:
            p = t.get('players', {}).get(who, {})
            nm = p.get('name', 'um jogador')
            t['last_winner_data'] = {
                'winner': None,
                'reason': 'Saldo insuficiente para reiniciar ({})'.format(nm),
                'cards': [],
                'round_history': []
            }
            t['_auto_restart_scheduled'] = False
            return

        # começa automaticamente uma nova mão (mesma mesa)
        iniciar_mao_truco(t)
        t['_auto_restart_scheduled'] = False

    Thread(target=_runner, daemon=True).start()


# Sons públicos (links)
SND_DEAL = "https://www.orangefreesounds.com/wp-content/uploads/2020/11/Dealing-cards-sound.mp3"
SND_FLIP = "https://www.orangefreesounds.com/wp-content/uploads/2018/07/Card-flip-sound-effect.mp3"
SND_WIN  = "https://orangefreesounds.com/wp-content/uploads/2025/05/Victory-sound-effect.mp3"

# -------------------------
# UTILITÁRIAS: BARALHO / FORÇA
# -------------------------
def baralho_truco():
    naipes = ['♦', '♠', '♥', '♣']
    valores = ['4', '5', '6', '7', 'Q', 'J', 'K', 'A', '2', '3']
    deck = [{'val': v, 'nai': n} for v in valores for n in naipes]
    random.shuffle(deck)
    return deck

def get_truco_power(card, vira):
    # Retorna métrica de força; manilha tem peso alto (100+)
    order = ['4', '5', '6', '7', 'Q', 'J', 'K', 'A', '2', '3']
    if not card:
        return -1
    try:
        idx_vira = order.index(vira.get('val')) if vira and vira.get('val') in order else order.index('4')
    except Exception:
        idx_vira = order.index('4')
    idx_manilha = (idx_vira + 1) % len(order)
    val_manilha = order[idx_manilha]
    if card.get('val') == val_manilha:
        naipe_power = ['♦', '♠', '♥', '♣'].index(card.get('nai'))
        return 100 + naipe_power
    try:
        return order.index(card.get('val'))
    except Exception:
        return -1

# -------------------------
# PRÓXIMO JOGADOR (ROBUSTO)
# -------------------------
def next_player(t, uid):
    po = t.get('player_order', [])
    if not po or uid not in po:
        return None
    played = {c.get('uid') for c in t.get('table_cards', [])}
    start = po.index(uid)
    for i in range(1, len(po)):
        cand = po[(start + i) % len(po)]
        if cand not in played:
            return cand
    return None

# -------------------------
# PONTOS / MÃO DE 11 (TRUCO PAULISTA)
# -------------------------
def _truco_team_points(t):
    """Retorna os pontos do placar por TIME (A/B).

    Preferência:
    1) t['team_points'] (fonte oficial)
    2) fallback: usa o MAIOR valor de points entre jogadores do mesmo time (evita somar em 2v2)
    """
    try:
        tp = t.get('team_points')
        if isinstance(tp, dict):
            return int(tp.get('A', 0) or 0), int(tp.get('B', 0) or 0)
    except Exception:
        pass

    pts = {'A': 0, 'B': 0}
    teams = t.get('teams') or {}
    for pid, p in (t.get('players') or {}).items():
        tm = teams.get(pid)
        if tm in pts:
            try:
                val = int(p.get('points') or 0)
            except Exception:
                val = 0
            if val > pts[tm]:
                pts[tm] = val
    return pts['A'], pts['B']

def _truco_team_first_player(t, team):
    # pega o primeiro jogador do time (útil para dar ponto em decisões como "correr")
    for pid in (t.get('player_order') or []):
        if (t.get('teams') or {}).get(pid) == team and pid in (t.get('players') or {}):
            return pid
    for pid in (t.get('players') or {}).keys():
        if (t.get('teams') or {}).get(pid) == team:
            return pid
    return None
# -------------------------
# PROCESSAMENTO DE PEDIDOS (TRUCO/ACEITAR/FUGIR)
# -------------------------
def processar_pedido_truco(t, uid, action):
    uid = str(uid)
    action = (action or "").strip()

    def _team(pid):
        teams = t.get("teams") or {}
        pid_s = str(pid)
        return teams.get(pid_s) or teams.get(pid)

    def _opp(team):
        return "B" if str(team) == "A" else "A"

    def _next_raise_value(v):
        v = int(v or 1)
        if v <= 1:
            return 3
        if v == 3:
            return 6
        if v == 6:
            return 9
        return 12

    # -------------------------------------------------------------
    # MÃO DE 11 (decisão do time com 11 pontos) - TRUCO PAULISTA
    # Regra escolhida por você: NINGUÉM pode pedir TRUCO/6/9/12 nessa mão.
    # -------------------------------------------------------------
    if action in ("hand11_play", "hand11_run"):
        mao = t.get("mao11") or {}
        if t.get("status") != "hand11_decision":
            return
        if not mao.get("active") or mao.get("type") != "normal":
            return

        team11 = mao.get("team11")
        if not team11:
            return

        if _team(uid) != team11:
            return  # só o time com 11 pode decidir

        if action == "hand11_play":
            mao["decision"] = "play"
            t["mao11"] = mao
            t["status"] = "playing"
            t["valor_mao"] = 3  # mão de 11 vale 3 (e não pode aumentar)
            t["truco_disabled"] = True  # nesta mão ninguém trucar

            if t.get("turn") == BOT_ID:
                Thread(target=ia_truco_jogar, args=(t,), daemon=True).start()
            return

        # hand11_run (correr): adversário ganha 1 ponto
        mao["decision"] = "run"
        t["mao11"] = mao
        t["truco_disabled"] = True
        t["truco_request"] = None
        t["valor_mao"] = 1

        opp_team = _opp(team11)
        opp_uid = _truco_team_first_player(t, opp_team)
        if opp_uid:
            finalizar_mao_truco(t, opp_uid)
        return

    # -------------------------------------------------------------
    # TRUCO / AUMENTOS (3/6/9/12) - TRUCO PAULISTA
    # Regras (como na mesa):
    # - Quem pede TRUCO (3) não pode pedir SEIS (6) depois do aceite.
    # - Somente o ADVERSÁRIO pode pedir 6.
    # - Depois do 6 aceito, o 9 volta para quem pediu TRUCO.
    # - Depois do 9 aceito, o 12 volta para o outro lado.
    #
    # Implementação:
    # - truco_can_raise_team: qual time pode pedir o PRÓXIMO aumento (em 'playing').
    #   None = qualquer time pode pedir o primeiro (TRUCO).
    # - truco_last_raise_team: último time que PEDIU o aumento aceito.
    # -------------------------------------------------------------
    if action in ("truco", "raise"):

        # ---------------------------------------------------------
        # 1) CONTRA-AUMENTO (respondendo a um pedido em 'waiting_truco')
        #    Ex.: TRUCO -> SEIS / SEIS -> NOVE / NOVE -> DOZE
        # ---------------------------------------------------------
        if t.get("status") == "waiting_truco" and t.get("truco_request"):
            if t.get("truco_disabled"):
                return

            tr = t.get("truco_request") or {}
            target = str(tr.get("target"))
            target_team = _team(target)
            my_team = _team(uid)

            # só o alvo (ou o parceiro do alvo) pode responder/contra-aumentar
            if uid != target:
                if not (my_team and target_team and str(my_team) == str(target_team)):
                    return

            cur_prop = int(tr.get("valor_proposto", 3))
            if cur_prop >= 12:
                return
            new_prop = _next_raise_value(cur_prop)
            if new_prop <= cur_prop:
                return

            rid = "{}-{}".format(int(time.time() * 1000), random.randint(1000, 9999))
            new_target = str(tr.get("author"))  # volta para quem pediu (parceiro também pode aceitar/correr)

            t["status"] = "waiting_truco"
            t["truco_request"] = {
                "author": uid,
                "target": new_target,
                "valor_proposto": new_prop,
                "rid": rid,
                "author_team": my_team
            }

            # BOT: pensa e aceita (com proteção contra request antigo)
            if str(new_target) == str(BOT_ID):
                def bot_delayed_accept(local_rid):
                    time.sleep(2.1)
                    tr2 = t.get("truco_request")
                    if t.get("status") == "waiting_truco" and tr2 and tr2.get("rid") == local_rid and str(tr2.get("target")) == str(BOT_ID):
                        processar_pedido_truco(t, BOT_ID, "accept")
                Thread(target=bot_delayed_accept, args=(rid,), daemon=True).start()
            return

        # ---------------------------------------------------------
        # 2) PEDIR TRUCO/SEIS/NOVE/DOZE (iniciando pedido, em 'playing')
        # ---------------------------------------------------------
        if t.get("status") != "playing":
            return
        if t.get("truco_disabled"):
            return
        if int(t.get("valor_mao", 1)) >= 12:
            return

        # só na sua vez
        if str(t.get("turn")) != uid:
            return

        # não abre um novo pedido se já existe um
        if t.get("truco_request"):
            return

        my_team = _team(uid)

        # alternância: só o time permitido pode pedir o PRÓXIMO aumento
        allowed_team = t.get("truco_can_raise_team")
        if allowed_team and my_team and str(my_team) != str(allowed_team):
            return

        novo_valor = _next_raise_value(int(t.get("valor_mao", 1)))

        # escolhe um alvo do time adversário (prioriza a próxima posição na ordem)
        uo_raw = t.get("player_order", []) or []
        uo = [str(x) for x in uo_raw]
        if uid not in uo:
            return
        idx = uo.index(uid)

        target = None
        for i in range(1, len(uo) + 1):
            cand = uo[(idx + i) % len(uo)]
            if _team(cand) != my_team:
                target = cand
                break
        if not target:
            return

        t["status"] = "waiting_truco"
        rid = "{}-{}".format(int(time.time() * 1000), random.randint(1000, 9999))
        t["truco_request"] = {
            "author": uid,
            "target": str(target),
            "valor_proposto": novo_valor,
            "rid": rid,
            "author_team": my_team
        }

        # BOT: pensa e aceita (com proteção contra request antigo)
        if str(target) == str(BOT_ID):
            def bot_delayed_accept(local_rid):
                time.sleep(2.1)
                tr2 = t.get("truco_request")
                if t.get("status") == "waiting_truco" and tr2 and tr2.get("rid") == local_rid and str(tr2.get("target")) == str(BOT_ID):
                    processar_pedido_truco(t, BOT_ID, "accept")
            Thread(target=bot_delayed_accept, args=(rid,), daemon=True).start()
        return

    # -------------------------------------------------------------
    # RESPOSTA AO PEDIDO (ACEITAR / CORRER)
    # - permite que o parceiro do alvo responda (modo 4 jogadores)
    # -------------------------------------------------------------
    if action in ("accept", "run"):
        if t.get("status") != "waiting_truco":
            return
        tr = t.get("truco_request")
        if not tr:
            return

        target = str(tr.get("target"))
        target_team = _team(target)
        my_team = _team(uid)

        # só o alvo ou alguém do mesmo time do alvo pode responder
        if uid != target:
            if not (my_team and target_team and str(my_team) == str(target_team)):
                return

        if action == "accept":
            proposed = int(tr.get("valor_proposto", t.get("valor_mao", 1)))
            t["valor_mao"] = proposed
            t["status"] = "playing"
            t["truco_request"] = None

            # registra o último time que pediu este aumento
            author_team = tr.get("author_team") or _team(tr.get("author"))
            t["truco_last_raise_team"] = author_team

            # alternância paulista: o PRÓXIMO aumento só pode vir do time adversário ao autor
            if author_team in ("A", "B"):
                t["truco_can_raise_team"] = _opp(author_team)
            else:
                # fallback seguro (não deveria acontecer)
                t["truco_can_raise_team"] = my_team or None

            if t.get("turn") == BOT_ID:
                Thread(target=ia_truco_jogar, args=(t,), daemon=True).start()
            return

        # action == "run" (correr do truco)
        winner = str(tr.get("author"))
        t["truco_request"] = None
        finalizar_mao_truco(t, winner)
        return

# -------------------------
# INÍCIO / FINAL DE MÃO
# -------------------------
def iniciar_mao_truco(t):
    t['deck'] = baralho_truco()
    t['status'] = 'playing'
    t['vira'] = t['deck'].pop() if t.get('deck') else None
    t['table_cards'] = []
    t['valor_mao'] = 1
    t['rodada_atual'] = 1
    t['truco_request'] = None
    t['last_winner_data'] = None
    t['round_history'] = []
    t['team_round_wins'] = {'A':0, 'B':0}
    t['played_cards_global'] = []
    t['_ia_busy'] = False  # flag para evitar threads concorrentes do bot

    # controle de aumentos (truco paulista) e mão de 11
    t['truco_last_raise_team'] = None
    t['truco_can_raise_team'] = None
    t['truco_disabled'] = False
    t['mao11'] = {'active': False}

    # mantém a ordem (humano em primeiro) quando já existe player_order
    uids = [uid for uid in (t.get('player_order') or []) if uid in (t.get('players', {}) or {})]
    for uid in (t.get('players', {}) or {}).keys():
        if uid not in uids:
            uids.append(uid)
    t['player_order'] = [str(x) for x in uids.copy()]

    # times fixos pela ordem (A/B/A/B ...)
    t['teams'] = {}
    for i, uid in enumerate(t.get('player_order', [])):
        t['teams'][uid] = 'A' if i % 2 == 0 else 'B'

    if not t.get('hand_player') or t.get('hand_player') not in t.get('player_order', []):
        t['hand_player'] = t.get('player_order', [None])[0]
    t['turn'] = t.get('hand_player')

    # distribui cartas
    for uid in t.get('player_order', []):
        t['players'][uid]['hand'] = [t['deck'].pop() for _ in range(3)] if t.get('deck') else []
        t['players'][uid]['round_wins'] = 0

    # -------------------------
    # MÃO DE 11 / MÃO DE FERRO
    # - nesta mão ninguém pode pedir truco
    # - mão de 11: time com 11 decide jogar ou correr (overlay no frontend)
    # - mão de ferro (11x11): ninguém vê as cartas (o servidor mascara no state)
    # -------------------------
    ptsA, ptsB = _truco_team_points(t)

    if ptsA == 11 and ptsB == 11:
        t['mao11'] = {'active': True, 'type': 'iron', 'team11': None, 'decision': 'play'}
        t['truco_disabled'] = True
        t['valor_mao'] = 3  # mão de ferro (11x11) vale 3 (sem aumentos)
        # segue direto para jogar (sem decisão), mas sem ver cartas (mascarado no state)
    else:
        team11 = None
        if ptsA == 11 and ptsB < 11:
            team11 = 'A'
        elif ptsB == 11 and ptsA < 11:
            team11 = 'B'

        if team11:
            t['mao11'] = {'active': True, 'type': 'normal', 'team11': team11, 'decision': None}
            t['truco_disabled'] = True
            t['valor_mao'] = 3  # mão de 11 vale 3 (sem aumentos)
            t['status'] = 'hand11_decision'  # pausa antes de começar a jogar

            # Se o TIME com 11 for do BOT (e não houver humano nesse time), o bot decide automaticamente.
            try:
                bot_team = (t.get('teams') or {}).get(BOT_ID)
                if bot_team and str(bot_team) == str(team11):
                    po = t.get('player_order') or []
                    teams_map = t.get('teams') or {}
                    has_human_same_team = any((str(pid) != str(BOT_ID) and str(teams_map.get(str(pid))) == str(team11)) for pid in po)
                    if not has_human_same_team:
                        def _bot_decide_hand11():
                            time.sleep(1.9)
                            if t.get('status') != 'hand11_decision':
                                return
                            mao_now = t.get('mao11') or {}
                            if not (mao_now.get('active') and mao_now.get('type') == 'normal' and mao_now.get('team11') == team11 and not mao_now.get('decision')):
                                return

                            hand = (t.get('players', {}).get(BOT_ID, {}).get('hand') or [])
                            powers = [get_truco_power(c, t.get('vira')) for c in hand if c]
                            mx = max(powers) if powers else -1
                            high = sum(1 for p in powers if p >= 7)  # A,2,3 (sem manilha)
                            mid = sum(1 for p in powers if p >= 5)   # J,K,A,2,3
                            # heurística simples: joga se mão forte, senão corre
                            if mx >= 100 or high >= 2 or mx >= 8 or (mid >= 2 and mx >= 6):
                                processar_pedido_truco(t, BOT_ID, "hand11_play")
                            else:
                                processar_pedido_truco(t, BOT_ID, "hand11_run")
                        Thread(target=_bot_decide_hand11, daemon=True).start()
            except Exception:
                pass

    # se começar com bot, dispara IA (apenas quando está realmente jogando)
    if t.get('status') == 'playing' and t.get('turn') == BOT_ID:
        Thread(target=ia_truco_jogar, args=(t,), daemon=True).start()

def finalizar_mao_truco(t, winner_uid, no_points=False):
    # ----------------------------
    # PLACAR OFICIAL (por time)
    # ----------------------------
    teams = t.get('teams') or {}
    if 'team_points' not in t or not isinstance(t.get('team_points'), dict):
        t['team_points'] = {'A': 0, 'B': 0}

    winner_uid = str(winner_uid) if winner_uid is not None else None
    winner_team = teams.get(winner_uid) if winner_uid else None

    # soma pontos da mão (se aplicável)
    if winner_team in ('A', 'B') and not no_points:
        try:
            t['team_points'][winner_team] = int(t['team_points'].get(winner_team, 0) or 0) + int(t.get('valor_mao', 1) or 1)
        except Exception:
            t['team_points'][winner_team] = (t['team_points'].get(winner_team, 0) or 0) + (t.get('valor_mao', 1) or 1)

        # mantém points dos jogadores sincronizados com o placar do time
        for pid, p in (t.get('players') or {}).items():
            tm = teams.get(pid)
            if tm in ('A', 'B'):
                p['points'] = int(t['team_points'].get(tm, 0) or 0)

    # snapshot para UI
    try:
        from copy import deepcopy
        cards_snapshot = deepcopy(t.get('table_cards', []))
    except Exception:
        cards_snapshot = list(t.get('table_cards', []))
    t['last_winner_data'] = {
        'winner': winner_uid,
        'winner_team': winner_team,
        'reason': 'Mão Finalizada',
        'cards': cards_snapshot,
        'round_history': list(t.get('round_history', []))
    }

    # fim da partida (12 pontos por time)
    try:
        ptsA, ptsB = _truco_team_points(t)
    except Exception:
        ptsA, ptsB = 0, 0

    if ptsA >= 12 or ptsB >= 12:
        t['status'] = 'finished'
        try:
            _truco_payout(t, winner_team=('A' if ptsA >= ptsB else 'B'))
        except Exception:
            try:
                _truco_payout(t)
            except Exception:
                pass
        return

    # animação curta de fim de mão e começa a próxima mão automaticamente
    t['status'] = 'round_end_anim'

    def restart_later():
        time.sleep(2.5)
        if t.get('status') != 'round_end_anim':
            return
        po = t.get('player_order', [])
        if po:
            cur = po.index(t.get('hand_player', po[0])) if t.get('hand_player') in po else 0
            t['hand_player'] = po[(cur + 1) % len(po)]
        iniciar_mao_truco(t)

    Thread(target=restart_later, daemon=True).start()

def resolver_rodada_truco(t):
    """
    Resolve a rodada atual (leva) com regras aprimoradas:
    - Identifica vencedor(s) da leva usando get_truco_power
    - Trata empates parciais e empate total
    - Atualiza round_history, team_round_wins e last_winner_data
    - Finaliza a mão quando as condições são atendidas
    - Garante que t['turn'] seja sempre definido para não travar o jogo
    - Dispara IA se for a vez do BOT_ID
    """
    try:
        played = t.get('table_cards', []) or []
        if not played:
            # nada a resolver
            return

        # quem começou a leva (primeira carta jogada nesta leva)
        starter_uid = played[0].get('uid')

        # calcula poder de cada carta
        powers = [(get_truco_power(c['card'], t.get('vira')), c['uid']) for c in played]
        max_power = max(p for p, uid in powers)
        winners = [uid for p, uid in powers if p == max_power]

        # inicializa estruturas se necessário
        if 'round_history' not in t:
            t['round_history'] = []
        if 'team_round_wins' not in t:
            t['team_round_wins'] = {'A': 0, 'B': 0}

        # utilitário para pegar um jogador representativo de um time
        def get_player_from_team(team):
            return next((uid for uid in t.get('player_order', []) if t.get('teams', {}).get(uid) == team), None)

        # decide vencedor da leva
        winner_team = None
        winner_uid = None

        if len(winners) == 1:
            winner_uid = winners[0]
            winner_team = t.get('teams', {}).get(winner_uid, None)
            # registra vitória parcial (leva) para jogador e time
            if winner_uid in t.get('players', {}):
                t['players'][winner_uid]['round_wins'] = t['players'][winner_uid].get('round_wins', 0) + 1
            if winner_team:
                t['team_round_wins'][winner_team] = t['team_round_wins'].get(winner_team, 0) + 1
            # passa a vez para o vencedor da leva (ele começa a próxima leva)
            t['turn'] = winner_uid
        else:
            # empate na leva (dois ou mais venceram)
            winner_team = 'D'  # draw
            # regra prática: quando há empate na leva, a vez volta para o jogador que iniciou a leva
            # (isso evita travamento). Em algumas variações a vez permanece com o iniciador da mão.
            # Nós definimos para starter_uid para manter consistência.
            t['turn'] = starter_uid

        # salva histórico desta leva
        t['round_history'].append(winner_team)

        # grava dados da leva para interface
        from copy import deepcopy
        t['last_winner_data'] = {
            'winner_team': winner_team,
            'winner_uid': winner_uid,
            'cards': deepcopy(played),
            'round_history': list(t.get('round_history', []))
        }

        # limpa cartas da mesa (foi levada)
        t['table_cards'] = []
        t['rodada_atual'] = t.get('rodada_atual', 1) + 1

        # avalia condição de fim de mão com regras robustas
        rh = t.get('round_history', [])
        wins_A = t['team_round_wins'].get('A', 0)
        wins_B = t['team_round_wins'].get('B', 0)

        # Função auxiliar para finalizar com um jogador de time
        def finish_for_team(team):
            player = get_player_from_team(team)
            if player:
                finalizar_mao_truco(t, player)
            else:
                # fallback: dá a mão ao hand_player se não encontrar representante
                finalizar_mao_truco(t, t.get('hand_player'))

        # 1) Se algum time já tem 2 vitórias, finaliza
        if wins_A >= 2:
            finish_for_team('A')
            return
        if wins_B >= 2:
            finish_for_team('B')
            return

        # 2) Situações com 2 rodadas jogadas:
        #    - Se houver empate na primeira e a segunda teve vencedor (D, A) => vencedor da segunda ganha
        if len(rh) == 2:
            # Regras clássicas (melhor de 3):
            # - Se a 1ª foi empate e a 2ª teve vencedor (D, A/B) => vencedor da 2ª decide a mão
            if rh[0] == 'D' and rh[1] != 'D':
                finish_for_team(rh[1])
                return

            # - Se a 1ª teve vencedor e a 2ª foi empate (A/B, D) => vencedor da 1ª decide a mão
            if rh[0] != 'D' and rh[1] == 'D':
                finish_for_team(rh[0])
                return

            # Caso contrário, continua para a 3ª (ex.: A,B) ou (D,D).


        # 3) Depois de 3 rodadas decidir por prioridade:
        if len(rh) >= 3:
            # caso todas empates -> empate total: ninguém pontua
            if all(x == 'D' for x in rh[:3]):
                # empate total da mão (nenhum pontua)
                finalizar_mao_truco(t, t.get('hand_player'), no_points=True)
                return
            # senão: quem aparecer primeiro nas 3 rodadas (não-D) decide
            for idx in range(3):
                if rh[idx] != 'D':
                    team = rh[idx]
                    finish_for_team(team)
                    return

        # 4) Se chegou até aqui: a mão continua (ninguém venceu ainda)
        t['status'] = 'playing'

        # garantia: se não há turn definido, seta para o próximo lógico (starter ou hand_player)
        if not t.get('turn'):
            # define para o jogador que iniciou a leva anterior ou para hand_player como fallback
            t['turn'] = starter_uid or t.get('hand_player')

        # dispara IA se for a vez do bot
        if t.get('turn') == BOT_ID and t.get('status') == 'playing':
            try:
                Thread(target=ia_truco_jogar, args=(t,), daemon=True).start()
            except Exception:
                # se não conseguir iniciar thread, loga e segue
                pass

    except Exception as e:
        # evita travar o servidor em caso de erro inesperado
        # registra no last_winner_data para debug sem quebrar o jogo
        t['last_winner_data'] = t.get('last_winner_data', {})
        t['last_winner_data']['resolve_error'] = str(e)
        # tenta colocar o jogo em estado jogável
        t['status'] = t.get('status', 'playing')
        if not t.get('turn'):
            t['turn'] = t.get('hand_player')


# -------------------------
# IA DO BOT APRIMORADA E SEGURO
# -------------------------
def ia_truco_jogar(t):
    try:
        BOT_ID = None
        for pid, p in t.get("players", {}).items():
            if p.get("is_bot"):
                BOT_ID = pid
                break

        if not BOT_ID:
            return

        # evita travamentos
        if not t.get("players") or not t.get("player_order"):
            return

        # se IA já está ocupada, ignora
        if t.get("_ia_busy", False):
            return

        t["_ia_busy"] = True
        time.sleep(0.85)

        # estados inválidos
        if t.get("status") not in ("playing", "waiting_truco"):
            return

        # -------------------------------
        #   BOT RESPONDENDO TRUCO
        # -------------------------------
        truco_req = t.get("truco_request")
        if t.get("status") == "waiting_truco" and truco_req and truco_req.get("target") == BOT_ID:
            # Regra do projeto: bot "pensa" um pouco e SEMPRE aceita (evita reinícios inesperados por correr do truco)
            rid = truco_req.get("rid")
            time.sleep(1.35)

            tr2 = t.get("truco_request") or {}
            if t.get("status") == "waiting_truco" and tr2 and tr2.get("rid") == rid and tr2.get("target") == BOT_ID:
                processar_pedido_truco(t, BOT_ID, "accept")
            return

        # -------------------------------
        #   BOT JOGANDO CARTA NORMAL
        # -------------------------------
        if t["status"] == "playing" and t["turn"] == BOT_ID:

            hand = t["players"][BOT_ID]["hand"]
            if not hand:
                return

            powers = [get_truco_power(c, t["vira"]) for c in hand]
            avg_power = sum(powers) / len(powers)
            manilhas = sum(1 for p in powers if p >= 100)

            # pode pedir truco
            if t["valor_mao"] == 1:
                if manilhas >= 1 and random.random() < 0.85:
                    processar_pedido_truco(t, BOT_ID, "truco")
                    return
                if avg_power >= 50 and random.random() < 0.45:
                    processar_pedido_truco(t, BOT_ID, "truco")
                    return

            # revalida mão
            hand = t["players"][BOT_ID]["hand"]
            if not hand:
                return

            # escolher carta
            if not t["table_cards"]:
                # primeira jogada
                if manilhas >= 2:
                    # joga a menor manilha
                    man_idxs = [i for i, p in enumerate(powers) if p >= 100]
                    carta_idx = man_idxs[0] if man_idxs else 0
                else:
                    # joga a carta média
                    sorted_idx = sorted(range(len(powers)), key=lambda i: powers[i])
                    carta_idx = sorted_idx[len(sorted_idx)//2]
            else:
                # responder jogada
                lead = t["table_cards"][0]["card"]
                lead_power = get_truco_power(lead, t["vira"])

                melhor_idx = None
                melhor_power = 9999
                pior_idx = 0
                pior_power = 9999

                for i, p in enumerate(powers):
                    if p < pior_power:
                        pior_power = p
                        pior_idx = i
                    if p > lead_power and p < melhor_power:
                        melhor_idx = i
                        melhor_power = p

                carta_idx = melhor_idx if melhor_idx is not None else pior_idx

            # jogar carta
            card = t["players"][BOT_ID]["hand"].pop(carta_idx)
            t["table_cards"].append({
                "uid": BOT_ID,
                "card": card,
                "name": t["players"][BOT_ID]["name"]
            })

            # próximo jogador
            next_uid = next_player(t, BOT_ID)

            if next_uid:
                t["turn"] = next_uid
                if next_uid == BOT_ID:  # bot joga novamente
                    Thread(target=ia_truco_jogar, args=(t,), daemon=True).start()
            else:
                # fim da rodada
                t["status"] = "resolving_round"

                def delayed():
                    time.sleep(1.4)
                    if t["status"] == "resolving_round":
                        resolver_rodada_truco(t)

                Thread(target=delayed, daemon=True).start()

    finally:
        t["_ia_busy"] = False



# -------------------------
# ROTAS API
# -------------------------
@truco_bp.route('/api/truco/create', methods=['POST'])
def api_truco_create():
    user = session.get('user')
    if not user:
        return jsonify({'erro': 'Não autenticado'}), 401
    data = request.json or {}
    try:
        bet = int(data.get('bet', 10))
        if bet <= 0:
            return jsonify({'erro': 'A aposta deve ser um valor positivo.'}), 400
    except (ValueError, TypeError):
        return jsonify({'erro': 'Valor da aposta é inválido.'}), 400
    name = data.get('name')
    if not name or not name.strip():
        return jsonify({'erro': 'Nome da mesa é obrigatório.'}), 400
    if name in TRUCO_TABLES:
        return jsonify({'erro': 'Uma mesa com este nome já existe.'}), 400
    TRUCO_TABLES[name] = {
        'name': name,
        'bet': bet,
        'owner': user['id'],
        'status': 'waiting',
        'players': {},
        'player_order': [],
        'teams': {},
        'team_points': {'A': 0, 'B': 0},
        'deck': [],
        'vira': None,
        'table_cards': [],
        'turn': None,
        'rodada_atual': 1,
        'valor_mao': 1,
        'truco_request': None,
        'last_winner_data': None,
        'round_history': [],
        'team_round_wins': {'A': 0, 'B': 0},
        'mao11': {'active': False, 'type': 'normal'},
        'truco_disabled': False,
        'truco_can_raise_team': None,
        'truco_last_raise_team': None,
        '_buyin_collected': False,
        '_paid_out': False
    }
    return jsonify({'ok': True})

@truco_bp.route('/api/truco/join', methods=['POST'])
def api_truco_join():
    user = session.get('user')
    if not user:
        return jsonify({'erro': 'Não autenticado'}), 401

    name = (request.json or {}).get('table')
    t = TRUCO_TABLES.get(name)

    if not t:
        return jsonify({'erro': 'Mesa não encontrada'}), 404

    # AUTO-RESET finished: evita cair direto na tela de "Vitória/Derrota" ao entrar numa mesa antiga
    try:
        if t.get('status') == 'finished':
            _truco_reset_match_state(t)
            t['last_winner_data'] = None
    except Exception:
        pass

    uid = str(user['id'])

    # garante estrutura
    if 'players' not in t:
        t['players'] = {}
    if 'player_order' not in t:
        t['player_order'] = []

    # limita a 4 jogadores
    if len(t['players']) >= 4 and uid not in t['players']:
        return jsonify({'erro': 'Mesa cheia'}), 400

    # adiciona jogador se não estiver presente
    if uid not in t['players']:
        t['players'][uid] = {
            'id': uid,
            'name': user.get('username', 'Player'),
            'avatar': user.get('avatar', ''),
            'hand': [],
            'round_wins': 0,
            'points': 0
        }

    # 🔥 assim garantimos que o jogador HUMANO fica SEMPRE em primeiro (BOTTOM)
    if uid in t['player_order']:
        t['player_order'].remove(uid)

    t['player_order'].insert(0, uid)

    return jsonify({'ok': True})


@truco_bp.route('/api/truco/join_bot', methods=['POST'])
def api_truco_join_bot():
    user = session.get('user')
    if not user:
        return jsonify({'erro': 'Não autenticado'}), 401

    data = request.json or {}
    name = data.get('table')
    t = TRUCO_TABLES.get(name)
    if not t:
        return jsonify({'erro': 'Mesa não encontrada'}), 404

    # AUTO-RESET finished: se a mesa ficou finalizada, prepara uma nova partida ao adicionar bot
    try:
        if t.get('status') == 'finished':
            _truco_reset_match_state(t)
            t['last_winner_data'] = None
    except Exception:
        pass

    t.setdefault('players', {})
    t.setdefault('player_order', [])

    # remove bots antigos (se houver)
    for pid, p in list(t['players'].items()):
        is_old_bot = (
            pid == BOT_ID or
            (isinstance(pid, str) and pid.startswith('BOT_')) or
            (isinstance(p, dict) and p.get('is_bot')) or
            ('jarvis' in str(p.get('name','')).lower())
        )
        if is_old_bot:
            t['players'].pop(pid, None)
            t['player_order'] = [x for x in t['player_order'] if x != pid]

    if len(t['players']) >= 4:
        return jsonify({'erro': 'Mesa cheia'}), 400

    bot_name = 'Jarvis Bot 🤖'
    bot_avatar = 'https://cdn-icons-png.flaticon.com/512/4712/4712100.png'

    # bot com ID fixo + flag is_bot=True (necessário para a IA e para os gatilhos)
    t['players'][BOT_ID] = {
        'id': BOT_ID,
        'name': bot_name,
        'avatar': bot_avatar,
        'hand': [],
        'round_wins': 0,
        'points': 0,
        'is_bot': True
    }

    # bots SEMPRE entram depois dos humanos
    t['player_order'] = [x for x in t['player_order'] if x != BOT_ID]
    t['player_order'].append(BOT_ID)

    # se já estiver jogando e for a vez do bot, dispara IA imediatamente
    if t.get('status') == 'playing' and t.get('turn') == BOT_ID:
        Thread(target=ia_truco_jogar, args=(t,), daemon=True).start()

    return jsonify({'ok': True})


@truco_bp.route('/api/truco/leave', methods=['POST'])
def api_truco_leave():
    data = request.get_json(silent=True) or {}
    name = data.get('name') or data.get('table') or data.get('mesa')
    user = session.get('user')
    if not user or not name:
        return jsonify({'ok': False, 'msg': 'missing'}), 400
    uid = user.get('id')
    t = TRUCO_TABLES.get(name)
    if not t:
        return jsonify({'ok': True})
    # remove jogador
    try:
        if uid in t.get('players', {}):
            t['players'].pop(uid, None)
        if uid in t.get('player_order', []):
            t['player_order'] = [x for x in t.get('player_order', []) if x != uid]
        # se era a vez dele, ajusta para o próximo
        if t.get('turn') == uid:
            t['turn'] = t['player_order'][0] if t.get('player_order') else None
        # mesa vazia? remove
        if not t.get('player_order'):
            TRUCO_TABLES.pop(name, None)
    except Exception:
        pass
    return jsonify({'ok': True})

@truco_bp.route('/api/truco/start', methods=['POST'])
def api_truco_start():
    user = session.get('user')
    if not user:
        return jsonify({'erro': 'Não autenticado'}), 401

    name = (request.json or {}).get('table')
    t = TRUCO_TABLES.get(name)

    if not t:
        return jsonify({'erro': 'Mesa não encontrada'}), 404

    uid = str(user['id'])

    # Apenas o dono da mesa pode iniciar
    if str(t.get("owner")) != uid:
        return jsonify({'erro': 'Apenas o dono da mesa pode iniciar a partida.'}), 403

    # evita "reiniciar" a partida sem querer
    if t.get('status') != 'waiting':
        return jsonify({'erro': 'A partida já está em andamento.'}), 400

    if len(t.get('players', {})) < 2:
        return jsonify({'erro': 'É necessário pelo menos 2 jogadores.'}), 400

    # -------------------------------------------------------------
    # INÍCIO ÚNICO (usa a mesma rotina do fluxo de mãos)
    # - garante teams / round_history / team_round_wins já na 1ª rodada
    # - mantém humano como primeiro na ordem
    # -------------------------------------------------------------
    t.setdefault('players', {})
    t.setdefault('player_order', list(t.get('players', {}).keys()))

    # garante que o dono/humano fique em primeiro
    po = [pid for pid in (t.get('player_order') or []) if pid in t.get('players', {})]
    for pid in (t.get('players', {}) or {}).keys():
        if pid not in po:
            po.append(pid)
    t['player_order'] = po

    if t.get('player_order'):
        t['hand_player'] = t['player_order'][0]

    # placar por time (fonte oficial)
    t['team_points'] = {'A': 0, 'B': 0}
    for pid, p in (t.get('players') or {}).items():
        try:
            tm = (t.get('teams') or {}).get(str(pid)) or (t.get('teams') or {}).get(pid)
            if tm in ('A','B'):
                p['points'] = 0
        except Exception:
            p['points'] = 0

    ok_buyin, who = _truco_collect_buyin(t)
    if not ok_buyin:
        # quem está sem saldo
        p = t.get('players', {}).get(who, {})
        nm = p.get('name', 'um jogador')
        return jsonify({'ok': False, 'err': 'Saldo insuficiente para iniciar ({} ).'.format(nm)})

    iniciar_mao_truco(t)
    return jsonify({'ok': True})


@truco_bp.route('/api/truco/action', methods=['POST'])
def api_truco_action():
    user = session.get('user')
    if not user:
        return jsonify({'erro': 'Não autenticado'}), 401
    data = request.json or {}
    t = TRUCO_TABLES.get(data.get('table'))
    if not t:
        return jsonify({'erro': 'Mesa não encontrada'}), 404
    processar_pedido_truco(t, str(user['id']), data.get('action'))
    return jsonify({'ok': True})

@truco_bp.route('/api/truco/play', methods=['POST'])
def api_truco_play():
    user = session.get('user')
    if not user:
        return jsonify({'erro': 'Não autenticado'}), 401
    data = request.json or {}
    t = TRUCO_TABLES.get(data.get('table'))
    if not t or t.get('status') != 'playing':
        return jsonify({'erro': 'Erro de estado'}), 400
    uid = str(user['id'])
    if t.get('turn') != uid:
        return jsonify({'erro': 'Não é sua vez'}), 400
    try:
        idx = int(data.get('idx'))
        player = t.get('players', {}).get(uid)
        if player is None:
            return jsonify({'erro': 'Jogador não encontrado'}), 400
        if idx < 0 or idx >= len(player.get('hand', [])):
            return jsonify({'erro': 'Índice de carta inválido'}), 400
        card = player['hand'].pop(idx)
    except Exception:
        return jsonify({'erro': 'Jogada inválida'}), 400

    t['table_cards'].append({'uid': uid, 'card': card, 'name': player.get('name')})

    # achar próximo jogador (robusto)
    next_uid = next_player(t, uid)
    if not next_uid:
        # todas cartas jogadas -> resolver
        t['status'] = 'resolving_round'
        def delayed_resolve():
            time.sleep(1.6)
            if t.get('status') == 'resolving_round':
                resolver_rodada_truco(t)
        Thread(target=delayed_resolve, daemon=True).start()
    else:
        t['turn'] = next_uid
        if t['turn'] == BOT_ID:
            Thread(target=ia_truco_jogar, args=(t,), daemon=True).start()
    return jsonify({'ok': True})


@truco_bp.route('/api/truco/state')
def api_truco_state():
    user = session.get('user')
    if not user:
        return jsonify({'erro': 'Não autenticado'}), 401
    t = TRUCO_TABLES.get(request.args.get('table'))
    if not t:
        return jsonify({'erro': 'Mesa não encontrada'}), 404

    uid = str(user['id'])
    teams = t.get('teams', {}) or {}
    requester_team = teams.get(uid)
    mao11 = t.get('mao11') or {'active': False}
    status = t.get('status')

    players_safe = []

    # show actual seats even when waiting: if seat empty, show placeholder
    player_order = t.get('player_order', [])
    if not player_order:
        player_order = list(t.get('players', {}).keys())

    for pid in player_order:
        pid_str = str(pid)
        p = t.get('players', {}).get(pid_str) or t.get('players', {}).get(pid)
        if p:
            real_hand = p.get('hand', []) or []
            masked_hand = [{'val': '', 'nai': '?'}] * len(real_hand)

            # VISIBILIDADE DE CARTAS:
            # - MÃO DE FERRO (11x11): ninguém vê nenhuma carta (nem a própria)
            # - MÃO DE 11 (decisão): time com 11 consegue ver as cartas do próprio time
            # - normal: cada um vê apenas as próprias cartas
            if mao11.get('active') and mao11.get('type') == 'iron':
                hand = masked_hand
            elif status == 'hand11_decision' and mao11.get('active') and mao11.get('type') == 'normal':
                team11 = mao11.get('team11')
                if requester_team and team11 and requester_team == team11 and teams.get(pid_str) == team11:
                    hand = real_hand  # mostra as cartas do time com 11 para quem está no time com 11
                else:
                    hand = real_hand if pid_str == uid else masked_hand  # pelo menos veja as próprias cartas
            else:
                hand = real_hand if pid_str == uid else masked_hand

            players_safe.append({
                'id': pid_str,
                'name': p.get('name'),
                'avatar': p.get('avatar'),
                'hand': hand,
                'points': p.get('points', 0),
                'round_wins': p.get('round_wins', 0)
            })
        else:
            players_safe.append({'id': None, 'name': 'Vaga', 'avatar': '', 'hand': [], 'points': 0, 'round_wins': 0})

    state = {
        'status': status,
        'turn': t.get('turn'),
        'vira': t.get('vira'),
        'valor_mao': t.get('valor_mao', 1),
        'rodada_atual': t.get('rodada_atual', 1),
        'table_cards': t.get('table_cards', []),
        'players': players_safe,
        'me_id': uid,
        'truco_request': t.get('truco_request'),
        'last_winner_data': t.get('last_winner_data'),
        'owner': t.get('owner'),
        'bet': t.get('bet'),
        'player_order': t.get('player_order', []),
        'teams': teams,
        'team_points': {'A': _truco_team_points(t)[0], 'B': _truco_team_points(t)[1]},
        'round_history': t.get('round_history', []),
        'team_round_wins': t.get('team_round_wins', {}),
        'truco_disabled': bool(t.get('truco_disabled')),
        'truco_can_raise_team': t.get('truco_can_raise_team'),
        'truco_last_raise_team': t.get('truco_last_raise_team'),
        'mao11': mao11
    }
    return jsonify(state)

# -------------------------
# INTERFACE (FRONTEND) - Estilo B (mesa verde escura)
# -------------------------
# -------------------------
@truco_bp.route('/game/truco')
def game_truco_ui():
    user = session.get('user')
    if not user:
        return redirect(url_for('login'))

    # monta lobby list (mostra jogadores nas suas posições)
    lobby_html = ""
    if not TRUCO_TABLES:
        lobby_html = "<div class='text-muted text-center p-5'>Nenhuma mesa de Truco foi criada.</div>"
    else:
        for nome, t in TRUCO_TABLES.items():
            # seats: mostra até 4 (preencher com vagas)
            seats_html = ""
            for p in t.get('players', {}).values():
                seats_html += '<div class="seat"><img src="{avatar}" title="{name}"></div>'.format(avatar=p.get('avatar',''), name=p.get('name',''))
            vagas = 4 - len(t.get('players', {}))
            for _ in range(vagas):
                seats_html += '<div class="seat empty-seat">+</div>'
            # montar bloco de mesa com escape seguro do nome
            lobby_html += (
                '<div class="table-card">'
                '<div class="table-header">{nome}</div>'
                '<div class="table-sub">{status} • ${bet}</div>'
                '<div class="seats">{seats}</div>'
                '<button class="btn btn-primary w-100" onclick="joinTable(\'{nome_esc}\')">Entrar</button>'
                '</div>'
            ).format(
                nome=nome,
                status=str(t.get('status','waiting')).upper(),
                bet=t.get('bet', 0),
                seats=seats_html,
                nome_esc=nome.replace("'", "")
            )

    # sons públicos (diretos)
    SND_DEAL = "https://www.orangefreesounds.com/wp-content/uploads/2020/11/Dealing-cards-sound.mp3"
    SND_FLIP = "https://www.orangefreesounds.com/wp-content/uploads/2018/07/Card-flip-sound-effect.mp3"
    SND_WIN  = "https://orangefreesounds.com/wp-content/uploads/2023/06/Victory-fanfare-sound-effect.mp3"

    # TRUCO: usamos speechSynthesis no frontend (mais estável), mas deixamos um mp3 opcional
    SND_TRUCO = "https://www.myinstants.com/media/sounds/truco.mp3"

    html_template = """
    <style>
    /* =========================================================
       LOBBY TRUCO (igual ao Blackjack)
       ========================================================= */
    .lobby-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 15px; margin-top: 15px; }

    .table-card {
        background: #15171c;
        border: 1px solid #2b2d33;
        padding: 15px;
        border-radius: 14px;
        animation: fadeIn 0.5s;
    }
    .table-header { font-size: 18px; font-weight: 700; color: #00eaff; }
    .table-sub { font-size: 12px; opacity: .8; margin-bottom: 10px; text-transform: uppercase; letter-spacing: .4px; }

    .seats { display: flex; gap: 8px; margin-bottom: 14px; min-height: 45px; align-items: center; }
    .seat {
        width: 45px; height: 45px; border-radius: 50%;
        background: #1f2229; display: flex; align-items: center; justify-content: center;
        overflow: hidden; border: 2px solid #333;
    }
    .seat img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .empty-seat { color: #666; font-size: 20px; font-weight: 800; cursor: default; }

    /* =========================================================
       TRUCO - AJUSTES DE LAYOUT (sem sobreposição)
       ========================================================= */
    .truco-table-felt { box-shadow: 0 18px 60px rgba(0,0,0,.55); position: relative; }

    /* =========================================================
       OVERLAYS (TRUCO / 11 / FIM DE PARTIDA)
       - Corrige bug de entrar na mesa e já aparecer “Vitória / Jogar novamente”
       ========================================================= */
    .truco-overlay{
        position:absolute; inset:0;
        display:none;
        align-items:center; justify-content:center;
        background: rgba(0,0,0,.62);
        z-index: 80;
        padding: 18px;
        pointer-events: none;
    }
    .truco-overlay.show{ display:flex; }
    .truco-overlay-card{
        width: min(520px, 92vw);
        background: rgba(12, 14, 20, .92);
        border: 1px solid rgba(255,255,255,.14);
        border-radius: 18px;
        padding: 16px 16px 14px;
        box-shadow: 0 22px 70px rgba(0,0,0,.68);
        backdrop-filter: blur(10px);
        text-align:center;
        pointer-events: none;
    }
    .truco-overlay-title{ font-weight: 900; font-size: 18px; letter-spacing: .3px; }
    .truco-overlay-sub{ opacity:.85; margin-top: 6px; font-size: 14px; }
    .truco-overlay-actions{ display:flex; gap:10px; justify-content:center; flex-wrap:wrap; margin-top: 14px; }

    /* Central: cartas jogadas em "slots" fixos */
    #table-center {
        top: 46%;
        width: min(620px, 88%);
        z-index: 20;
    }
    #center-cards-container {
        display: flex;
        justify-content: center;
        align-items: flex-end;
        gap: 18px;
        flex-wrap: wrap;
        min-height: 150px; /* reserva espaço para não encostar nos botões */
    }
    .played-card-box {
        width: 120px;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
    }
    .played-card-box img {
        width: 92px;
        border-radius: 8px;
        box-shadow: 0 10px 18px rgba(0,0,0,.35);
    }
    .played-name {
        max-width: 120px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: 12px;
        opacity: .86;
        padding: 2px 8px;
        border-radius: 999px;
        background: rgba(0,0,0,.28);
        border: 1px solid rgba(255,255,255,.10);
    }

    /* Meu nome abaixo das minhas cartas (slot bottom é sempre o usuário) */
    #player-bottom { flex-direction: column-reverse; gap: 8px; z-index: 15; }
    #player-bottom .player-hand { margin: 0; }

    /* Botão TRUCO fora do centro (evita sobrepor cartas) */
    .action-btns {
        position: absolute;
        right: 18px;
        bottom: 18px;
        left: auto;
        transform: none;
        z-index: 35;
        display: flex;
        gap: 10px;
        align-items: center;
        pointer-events: auto;
    }

    /* Mensagens/controles no canto inferior esquerdo (sem centralizar) */
    #owner-controls.owner-controls {
        position: absolute;
        left: 18px !important;
        bottom: 18px !important;
        top: auto !important;
        right: auto !important;
        transform: none !important;
        text-align: left;
        z-index: 34;
        display: flex;
        flex-direction: column;
        gap: 10px;
        pointer-events: auto !important;
    }

    @media (max-width: 720px) {
        #table-center { top: 44%; width: min(560px, 92%); }
        .action-btns { left: 50%; right: auto; transform: translateX(-50%); bottom: 12px; }
        #owner-controls.owner-controls { left: 12px !important; bottom: 12px !important; }
        #center-cards-container { min-height: 160px; }
    }
    

    /* --- Fim do jogo (overlay grande) --- */
    .endgame-overlay{pointer-events:none;}
    .endgame-overlay.show{pointer-events:auto;}
    .endgame-overlay .truco-overlay-card{background:rgba(0,0,0,.55); border:1px solid rgba(255,255,255,.18); padding:22px 26px; min-width:min(520px, 92vw); text-align:center;}
    .endgame-overlay.win .truco-overlay-title{color:#21c37b;}
    .endgame-overlay.lose .truco-overlay-title{color:#ff4d4f;}
    .endgame-actions{margin-top:14px; display:flex; gap:12px; justify-content:center; pointer-events:auto;}
    .endgame-actions .btn{padding:10px 16px; border-radius:12px; font-weight:800;}

    </style>

    <div id="truco-lobby" class="card p-3" style="max-width:1100px;margin:0 auto;">
        <h5>🃏 Lobby Truco</h5>
        <div class="mb-2 d-flex gap-2">
            <input id="t-name" class="form-control" placeholder="Nome" value="Mesa-__USERNAME__">
            <input id="t-bet" type="number" class="form-control" style="width:100px" placeholder="Bet" value="10">
            <button class="btn btn-primary" onclick="createTruco()">Criar</button>
        </div>
        <hr><h3 class="mt-3">Mesas</h3>
        <div id="lobby-list" class="lobby-grid">__LOBBY_HTML__</div>
        <button class="btn btn-outline-secondary w-100 mt-2" onclick="refreshTrucoTables()">Atualizar</button>
    </div>

   <div id="truco-game"  style="display:none; max-width:1100px; margin:18px auto;">
    <div class="truco-table-felt">

        <!-- Placar -->
        <div id="placar-area">
            <div class="placar-title">VALOR DA MÃO: <b id="val-mao">1</b></div>
            <div id="placar-txt">Nós 0 × 0 Eles</div>
            <div id="round-dots-area" class="round-dots"></div>
        </div>

        <!-- Carta Vira -->
        <div id="vira-area">
            <div class="vira-title">VIRA</div>
            <div id="vira-img-container"></div>
        </div>

        <!-- Mesa central -->
        <div id="table-center">
            <div id="center-cards-container"></div>
        </div>

        <!-- Jogadores -->
        <div id="player-top" class="player-area pos-top"></div>
        <div id="player-left" class="player-area pos-left"></div>
        <div id="player-right" class="player-area pos-right"></div>
        <div id="player-bottom" class="player-area pos-bottom"></div>

        <!-- Ações do jogador -->
        <div id="my-action-btns" class="action-btns"></div>

        <!-- Mensagens (vez, aviso etc.) -->
        <div id="owner-controls" class="owner-controls"></div>

        <!-- Sons -->
        <audio id="snd-deal" preload="auto" src="__SND_DEAL__"></audio>
        <audio id="snd-flip" preload="auto" src="__SND_FLIP__"></audio>
        <audio id="snd-win" preload="auto" src="__SND_WIN__"></audio>
    <audio id="snd-truco" preload="auto" src="__SND_TRUCO__"></audio>

    <div id="truco-overlay" class="truco-overlay">
        <div class="truco-overlay-card">
            <div id="truco-overlay-title" class="truco-overlay-title">TRUCO!</div>
            <div id="truco-overlay-sub" class="truco-overlay-sub"></div>
        </div>
    </div>



    <div id="hand11-overlay" class="truco-overlay hand11-overlay">
        <div class="truco-overlay-card">
            <div class="truco-overlay-title" id="hand11-title">MÃO DE 11</div>
            <div class="truco-overlay-sub" id="hand11-sub"></div>
            <div class="endgame-actions" id="hand11-actions"></div>
        </div>
    </div>

    <div id="endgame-overlay" class="truco-overlay endgame-overlay">
        <div class="truco-overlay-card">
            <div id="endgame-title" class="truco-overlay-title">VITÓRIA!</div>
            <div id="endgame-sub" class="truco-overlay-sub"></div>
            <div class="endgame-actions">
                <button class="btn btn-primary" onclick="playAgain()">Jogar novamente</button>
            </div>
        </div>
    </div>

    </div>
</div>


    <script data-cfasync="false">
    const USER_ID = "__USER_ID__";
    let CURRENT_TABLE = null, POLLING = null, LAST_JSON = "";

    // Botão "Sair da mesa"
    function ensureLeaveBtn(){
        try{
            let b = document.getElementById('btn-leave-truco');
            if(!b){
                b = document.createElement('button');
                b.id = 'btn-leave-truco';
                b.className = 'btn btn-sm btn-outline-warning';
                b.style.position='fixed';
                b.style.top='86px';
                b.style.right='18px';
                b.style.zIndex='9999';
                b.style.display='none';
                b.textContent='Sair da mesa';
                b.onclick = async ()=>{
                    if(!CURRENT_TABLE) return;
                    await apiCall('/api/truco/leave', {name: CURRENT_TABLE});
                    CURRENT_TABLE = null; ensureLeaveBtn();
                    try{ clearInterval(POLLING); }catch(e){}
                    location.href='/game/truco';
                };
                document.body.appendChild(b);
            }
            b.style.display = CURRENT_TABLE ? 'inline-block' : 'none';
        }catch(e){}
    }

    window.addEventListener('load', () => { try { refreshTrucoTables(); } catch(e){} try{ ensureLeaveBtn(); }catch(e){} });

    function getCardURL(val, nai) {
        if(!val || !nai) return "https://deckofcardsapi.com/static/img/back.png";
        const suit = {'♥':'H','♦':'D','♣':'C','♠':'S'}[nai];
        try { return "https://deckofcardsapi.com/static/img/" + val + suit + ".png"; } catch(e) { return "https://deckofcardsapi.com/static/img/back.png"; }
    }

    async function apiCall(endpoint, body) {
        try {
            const res = await fetch(endpoint, { method: 'POST', headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
            const data = await res.json();
            if (!res.ok) { alert("Erro: " + (data.erro || 'Ocorreu um problema')); return null; }
            return data;
        } catch (err) {
            if(!endpoint.includes('/state')) alert("Erro de conexão com o servidor.");
            return null;
        }
    }

    async function refreshTrucoTables() {
        try {
            const res = await fetch('/api/truco/list');
            const mesas = await res.json();
            const container = document.getElementById('lobby-list');
            if(!container) return;

            const html = (mesas && mesas.length) ? mesas.map(m => {
                let seatsHtml = '';
                (m.players || []).forEach(p => { seatsHtml += `<div class="seat"><img src="${p.avatar}"></div>`; });

                const maxP = m.max_players || 4;
                const vagas = maxP - (m.players || []).length;
                for(let i = 0; i < vagas; i++) seatsHtml += '<div class="seat empty-seat">+</div>';

                const status = String(m.status || 'waiting').toUpperCase();
                const bet = (m.bet == null ? 0 : m.bet);

                return `<div class="table-card">
                    <div class="table-header">${m.name}</div>
                    <div class="table-sub">${status} • $${bet}</div>
                    <div class="seats">${seatsHtml}</div>
                    <button class="btn btn-primary w-100" onclick='joinTable(${JSON.stringify(m.name)})'>Entrar</button>
                </div>`;
            }).join('') : "<div class='text-muted text-center p-5'>Nenhuma mesa.</div>";

            container.innerHTML = html;
        } catch(e) {}
    }

    async function createTruco() {
        const name = document.getElementById('t-name').value; const bet = document.getElementById('t-bet').value;
        if(!name || !bet) return;
        const data = await apiCall('/api/truco/create', {name, bet});
        if(data && data.ok) await refreshTrucoTables();
    }

    async function joinTable(name) {
        const data = await apiCall('/api/truco/join', {table: name});
        if(data && data.ok) { CURRENT_TABLE = name; ensureLeaveBtn(); showGame(); startPolling(); }
    }

    async function addBot() { await apiCall('/api/truco/join_bot', {table: CURRENT_TABLE}); }
    async function playCard(idx) { await apiCall('/api/truco/play', {table: CURRENT_TABLE, idx}); playSoundElem('snd-flip'); }
    async function startMatch() { await apiCall('/api/truco/start', {table: CURRENT_TABLE}); playSoundElem('snd-deal'); }
    function nextTrucoValue(v){
        const n = Number(v || 1);
        if(n <= 1) return 3;
        if(n === 3) return 6;
        if(n === 6) return 9;
        return 12;
    }
    function trucoLabelForValue(v){
        const n = Number(v || 1);
        if(n === 3) return "TRUCO";
        if(n === 6) return "SEIS";
        if(n === 9) return "NOVE";
        if(n === 12) return "DOZE";
        return "TRUCO";
    }
    async function sendAction(act) {
        // pedir TRUCO/AUMENTAR: toca som, mostra overlay e guarda o valor proposto
        if (act === 'truco' || act === 'raise') {
            TRUCO_WAITING_ACCEPT = true;

            // base padrão: valor atual da mão
            let base = (LAST_VALOR_MAO || 1);

            // contra-aumento (estou respondendo a um pedido na tela)
            const counterRaise = (act === 'raise' && String(LAST_STATUS) === 'waiting_truco' && window.__TRUCO_PENDING_VALOR_PROPOSTO);
            if (counterRaise) {
                base = Number(window.__TRUCO_PENDING_VALOR_PROPOSTO || base);

                // é uma resposta: corta o som do pedido anterior imediatamente
                TRUCO_LOCAL_RESPONDED = true;
                stopTrucoSound();
                playAcceptSfx();
            } else {
                TRUCO_LOCAL_RESPONDED = false;
            }

            TRUCO_LAST_PROPOSED = nextTrucoValue(base);

            const lbl = trucoLabelForValue(TRUCO_LAST_PROPOSED) + "!";
            playTrucoSound(lbl);
            showTrucoOverlay(lbl, 'Aguardando resposta...', 0);
        }

        // respondendo a um truco: interrompe som do TRUCO imediatamente e toca "aceito/não"
        if (act === 'accept') {
            TRUCO_LOCAL_RESPONDED = true;
            stopTrucoSound();
            playAcceptSfx();
            hideTrucoOverlay();
            showTrucoOverlay('ACEITO!', 'Bora jogar!', 650);
        } else if (act === 'run') {
            TRUCO_LOCAL_RESPONDED = true;
            stopTrucoSound();
            // "medroso" = recusar/correr
            playCowardSfx();
            hideTrucoOverlay();
            showTrucoOverlay('CORREU!', 'Ok!', 650);
        }

        await apiCall('/api/truco/action', {table: CURRENT_TABLE, action: act});
    }
    function leaveTruco() { location.reload(); }

    function showGame() {
        document.getElementById('truco-lobby').style.display = 'none';
        document.getElementById('truco-game').style.display = 'block';

        // garante que não “herda” overlays visíveis ao entrar na mesa
        hideTrucoOverlay();
        hideHand11Overlay();
        hideEndgameOverlay();
        GAME_END_SHOWN = false;
        GAME_END_SOUND_PLAYED = false;
    }

    function startPolling() {
        if (POLLING) clearInterval(POLLING);
        POLLING = setInterval(async () => {
            try {
                if (!CURRENT_TABLE) return;
                const res = await fetch("/api/truco/state?table=" + encodeURIComponent(CURRENT_TABLE));
                if(res.status !== 200) return;
                const state = await res.json();
                if(JSON.stringify(state) === LAST_JSON) return;
                LAST_JSON = JSON.stringify(state);
                renderTable(state);
            } catch(e) {}
        }, 700);
    }

    let LAST_TRUCO_RID = null;
let TRUCO_WAITING_ACCEPT = false;
let TRUCO_LAST_PROPOSED = null;
let TRUCO_SOUND_ACTIVE = false;
let TRUCO_LOCAL_RESPONDED = false;
let LAST_STATUS = '';
let LAST_TRUCO_ACTIVE = false;
let LAST_VALOR_MAO = 1;

let TRUCO_OVERLAY_TIMER = null;
let LAST_RH_LEN = 0;
let LAST_HAND_WINNER = null;
// desbloqueia áudio no 1º clique (política de autoplay)
document.addEventListener('click', () => { try { getSfxCtx(); } catch(e){} }, { once: true });

function showTrucoOverlay(title, sub, autoHideMs){
    const ov = document.getElementById('truco-overlay');
    if(!ov) return;
    const t = document.getElementById('truco-overlay-title');
    const s = document.getElementById('truco-overlay-sub');
    if(t) t.textContent = title || 'TRUCO!';
    if(s) s.textContent = sub || '';
    ov.classList.add('show');
    if(TRUCO_OVERLAY_TIMER) clearTimeout(TRUCO_OVERLAY_TIMER);
    if(autoHideMs && autoHideMs > 0){
        TRUCO_OVERLAY_TIMER = setTimeout(()=>{ ov.classList.remove('show'); TRUCO_OVERLAY_TIMER = null; }, autoHideMs);
    }
}

function hideTrucoOverlay(){
    const ov = document.getElementById('truco-overlay');
    if(!ov) return;
    if(TRUCO_OVERLAY_TIMER){ clearTimeout(TRUCO_OVERLAY_TIMER); TRUCO_OVERLAY_TIMER = null; }
    ov.classList.remove('show');
}

function showHand11Overlay(subHtml, actionsHtml){
    const ov = document.getElementById('hand11-overlay');
    const sub = document.getElementById('hand11-sub');
    const act = document.getElementById('hand11-actions');
    if (!ov || !sub || !act) return;
    sub.innerHTML = subHtml || '';
    act.innerHTML = actionsHtml || '';
    ov.classList.add('show');
}
function hideHand11Overlay(){
    const ov = document.getElementById('hand11-overlay');
    const sub = document.getElementById('hand11-sub');
    const act = document.getElementById('hand11-actions');
    if (!ov) return;
    ov.classList.remove('show');
    if (sub) sub.innerHTML = '';
    if (act) act.innerHTML = '';
}

function playTrucoSound(callText){
    TRUCO_SOUND_ACTIVE = true;
    // 1) tenta mp3
    try{
        const el = document.getElementById('snd-truco');
        if(el){ el.currentTime = 0; el.volume = 0.95; el.play().catch(()=>{}); }
    }catch(e){}
    // 2) fallback por voz (bem estável)
    try{
        if('speechSynthesis' in window){
            const u = new SpeechSynthesisUtterance(String(callText || 'Truco!'));
            u.lang = 'pt-BR';
            u.rate = 1.0;
            window.speechSynthesis.cancel();
            window.speechSynthesis.speak(u);
        }
    }catch(e){}
}



function stopTrucoSound(){
    try{
        const el = document.getElementById('snd-truco');
        if(el){ el.pause(); el.currentTime = 0; }
    }catch(e){}
    try{
        if('speechSynthesis' in window){ window.speechSynthesis.cancel(); }
    }catch(e){}
    TRUCO_SOUND_ACTIVE = false;
}

function playAcceptSfx(){
    try{
        // "aceito": dois toques curtos subindo (bem curto e agradável)
        sfxBeep(740, 0.10, 'triangle', 0.055, 0.00);
        sfxBeep(988, 0.12, 'triangle', 0.050, 0.11);
    }catch(e){}
}
function playRefuseSfx(){
    try{
        // "não": dois toques curtos descendo
        sfxBeep(240, 0.10, 'square', 0.050, 0.00);
        sfxBeep(170, 0.12, 'square', 0.045, 0.11);
    }catch(e){}
}


// ---- SFX curtos (sem depender de links) ----
function getSfxCtx(){
    const AC = window.AudioContext || window.webkitAudioContext;
    if(!AC) return null;
    if(!window.__SFX_CTX) window.__SFX_CTX = new AC();
    const ctx = window.__SFX_CTX;
    if(ctx && ctx.state === 'suspended'){ ctx.resume().catch(()=>{}); }
    return ctx;
}
function sfxBeep(freq, dur, type, gain, when){
    const ctx = getSfxCtx();
    if(!ctx) return;
    const t0 = ctx.currentTime + (when || 0);
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = type || 'sine';
    o.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(gain || 0.07, t0 + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + (dur || 0.12));
    o.connect(g); g.connect(ctx.destination);
    o.start(t0);
    o.stop(t0 + (dur || 0.12) + 0.02);
}
function playDefeatSfx(){
    try{
        // queda rápida (curto e bem claro)
        sfxBeep(440, 0.12, 'triangle', 0.08, 0.00);
        sfxBeep(330, 0.14, 'triangle', 0.07, 0.12);
        sfxBeep(220, 0.18, 'triangle', 0.07, 0.26);
    }catch(e){}
}
function playCowardSfx(){
    try{
        // "medroso": um tremidinho rápido descendo
        const ctx = getSfxCtx(); if(!ctx) return;
        const t = ctx.currentTime;

        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.type = 'sawtooth';
        o.frequency.setValueAtTime(520, t);
        o.frequency.exponentialRampToValueAtTime(260, t + 0.18);

        const lfo = ctx.createOscillator();
        const lfoG = ctx.createGain();
        lfo.frequency.setValueAtTime(18, t);
        lfoG.gain.setValueAtTime(30, t);
        lfo.connect(lfoG);
        lfoG.connect(o.frequency);

        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.07, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);

        o.connect(g); g.connect(ctx.destination);
        o.start(t); lfo.start(t);
        o.stop(t + 0.24); lfo.stop(t + 0.24);
    }catch(e){}
}

function playSoundElem(id, maxMs) {
        try {
            const el = document.getElementById(id);
            if(!el) return;
            try { el.pause(); } catch(e){}
            el.currentTime = 0;
            // Vitória: mais baixa e curta
            el.volume = (id === 'snd-win') ? 0.12 : 0.9;
            el.play().catch(()=>{});
            const cut = (typeof maxMs === 'number' && maxMs > 0) ? maxMs : ((id === 'snd-win') ? 650 : 0);
            if(cut > 0){
                setTimeout(()=>{ try{ el.pause(); }catch(e){} }, cut);
            }
        } catch(e){}
    }

let GAME_END_SHOWN = false;
let GAME_END_SOUND_PLAYED = false;

function showEndgameOverlay(win, ptsA, ptsB){
    const ov = document.getElementById('endgame-overlay');
    if(!ov) return;
    ov.classList.add('show');
    ov.classList.toggle('win', !!win);
    ov.classList.toggle('lose', !win);

    const t = document.getElementById('endgame-title');
    const s = document.getElementById('endgame-sub');
    if(t) t.textContent = win ? 'VITÓRIA!' : 'DERROTA!';
    if(s) s.textContent = 'Placar final: ' + String(ptsA) + ' × ' + String(ptsB);

    if(!GAME_END_SOUND_PLAYED){
        GAME_END_SOUND_PLAYED = true;
        stopTrucoSound();
        if(win) playGameWinSfx(); else playGameLoseSfx();
    }
}

function hideEndgameOverlay(){
    const ov = document.getElementById('endgame-overlay');
    if(!ov) return;
    ov.classList.remove('show','win','lose');
    GAME_END_SOUND_PLAYED = false;
}

function playGameWinSfx(){
    try{
        // acorde curtinho e "triunfal"
        sfxBeep(523, 0.12, 'triangle', 0.09, 0.00);
        sfxBeep(659, 0.14, 'triangle', 0.08, 0.10);
        sfxBeep(784, 0.16, 'triangle', 0.08, 0.22);
        sfxBeep(1046,0.18, 'triangle', 0.07, 0.36);
    }catch(e){}
}
function playGameLoseSfx(){
    try{
        // queda curta (game over)
        sfxBeep(392, 0.14, 'sine', 0.08, 0.00);
        sfxBeep(294, 0.16, 'sine', 0.08, 0.14);
        sfxBeep(220, 0.20, 'sine', 0.08, 0.30);
    }catch(e){}
}

async function playAgain(){
    try{
        if(!CURRENT_TABLE) { location.reload(); return; }

        // reinicia a partida (mesma mesa) e inicia novamente
        await apiCall('/api/truco/restart', {table: CURRENT_TABLE});
        await apiCall('/api/truco/start', {table: CURRENT_TABLE});

        // reseta controles de UI/som
        LAST_HAND_WINNER = null;
        LAST_RH_LEN = 0;
        hideEndgameOverlay();

        // renderiza o novo estado sem recarregar a página
        try{
            const res = await fetch("/api/truco/state?table=" + encodeURIComponent(CURRENT_TABLE));
            const js = await res.json();
            if(js && js.ok) renderTable(js.state);
        }catch(e){}
    }catch(e){
        location.reload();
    }
}

function renderTable(s) {
    if (!s) return;

    const prevStatus = LAST_STATUS;
    const prevValorMao = LAST_VALOR_MAO;
    const prevTrucoActive = LAST_TRUCO_ACTIVE;
    const trucoActiveNow = (s.status === 'waiting_truco' && !!s.truco_request);

    // detecta fim do TRUCO para cortar o som e tocar "aceito/não"
    if (prevTrucoActive && !trucoActiveNow) {
        stopTrucoSound();
        // evita tocar duas vezes no cliente que acabou de clicar em aceitar/correr
        if (!TRUCO_LOCAL_RESPONDED) {
            const accepted = (s.status === 'playing' && Number(s.valor_mao || 1) > Number(prevValorMao || 1));
            if (accepted) playAcceptSfx();
            else playRefuseSfx();
        }
        TRUCO_LOCAL_RESPONDED = false;
    }

    LAST_STATUS = String(s.status || '');
    LAST_TRUCO_ACTIVE = !!trucoActiveNow;

    const USER = String(USER_ID);
    const player_order = s.player_order || [];
    const players = s.players || [];

    // guarda minha mão mais recente (pra ela não "sumir" durante o pedido de truco)
    window.__LAST_MY_HAND = window.__LAST_MY_HAND || null;
    const meNow = players.find(pp => pp && String(pp.id) === USER);
    if (meNow && Array.isArray(meNow.hand) && meNow.hand.length) {
        window.__LAST_MY_HAND = meNow.hand.map(c => ({ val: c.val, nai: c.nai }));
    }

    const numPlayers = player_order.length;

    // ------------------------------
    // 1. LIMPAR ÁREAS
    // ------------------------------
    ["bottom", "top", "left", "right"].forEach(pos => {
        const el = document.getElementById("player-" + pos);
        if (el) el.innerHTML = "";
    });

    document.getElementById("center-cards-container").innerHTML = "";

    // ------------------------------
    // 2. POSICIONAR CADA JOGADOR
    // ------------------------------
    const myIndex = player_order.indexOf(USER);
    const posMap = {};

    for (let i = 0; i < player_order.length; i++) {
        const pid = String(player_order[i]);
        let p = players.find(pp => String(pp.id) === pid);

        if (!p) p = { id: pid, name: "Aguardando...", avatar: "", hand: [] };

        const rel = (i - myIndex + numPlayers) % numPlayers;
        let slot = "";

        if (rel === 0) slot = "bottom";
        else if (numPlayers === 2) slot = "top";
        else if (numPlayers === 3 && rel === 1) slot = "left";
        else if (numPlayers === 3 && rel === 2) slot = "right";
        else if (numPlayers === 4 && rel === 1) slot = "left";
        else if (numPlayers === 4 && rel === 2) slot = "top";
        else if (numPlayers === 4 && rel === 3) slot = "right";

        posMap[slot] = p;
    }

    // ------------------------------
    // 3. DESENHAR JOGADORES
    // ------------------------------
    for (const pos in posMap) {
        const p = posMap[pos];
        const isMe = String(p.id) === USER;
        const el = document.getElementById("player-" + pos);

        if (!el) continue;

        // Avatar + nome
        let html = `
            <div class="player-avatar-name">
                <img src="${p.avatar}">
                <span>${p.name}</span>
            </div>
        `;

        // Cartas da mão
        if (s.status === "playing" || s.status === "resolving_round" || s.status === "waiting_truco" || s.status === "hand11_decision") {
            const canPlay = (s.turn === USER && isMe && s.status === "playing");

            let handList = (p.hand || []);
            if (isMe && (!handList || handList.length === 0) && s.status === "waiting_truco" && window.__LAST_MY_HAND && window.__LAST_MY_HAND.length) {
                handList = window.__LAST_MY_HAND;
            }

            const cardsHTML = handList.map((c, i) => {
                let url;

                if (isMe) {
                    url = getCardURL(c.val, c.nai);
                } else {
                    url = "https://deckofcardsapi.com/static/img/back.png";
                }

                return `
                    <img src="${url}"
                         class="card-img ${isMe ? "my-card" : ""}"
                         onclick="${canPlay ? `playCard(${i})` : ""}">
                `;
            }).join("");

            html += `<div class="player-hand">${cardsHTML}</div>`;
        }

        el.innerHTML = html;
    }

    // ------------------------------
    // 4. EXIBIR VIRA
    // ------------------------------
    if (s.vira) {
        document.getElementById("vira-img-container").innerHTML =
            `<img src="${getCardURL(s.vira.val, s.vira.nai)}" class="card-img">`;
    }

    // ------------------------------
    // 5. CARTAS NA MESA (CENTRO)
    // ------------------------------
    const cc = document.getElementById("center-cards-container");
    cc.innerHTML = "";

    (s.table_cards || []).forEach(entry => {
        const card = entry.card;
        const cardUrl = getCardURL(card.val, card.nai);

        const div = document.createElement("div");
        div.className = "played-card-box";

        div.innerHTML = `
            <img src="${cardUrl}">
            <div class="played-name">${entry.name || ""}</div>
        `;

        cc.appendChild(div);
    });

    // ------------------------------
    // 6. PLACAR
    // ------------------------------
    const myTeam = s.teams ? s.teams[USER] : null;
    let ptsA = 0, ptsB = 0;

    // usa placar oficial por time (evita somar em 2v2)
    if (s.team_points) {
        ptsA = Number(s.team_points.A || 0);
        ptsB = Number(s.team_points.B || 0);
    } else {
        players.forEach(p => {
            const t = s.teams ? s.teams[p.id] : null;
            if (!t) return;
            const v = Number(p.points || 0);
            if (t === "A") ptsA = Math.max(ptsA, v);
            else ptsB = Math.max(ptsB, v);
        });
    }

    const vmEl = document.getElementById("val-mao");
    if (vmEl) vmEl.textContent = String(s.valor_mao || 1);
    LAST_VALOR_MAO = Number(s.valor_mao || 1);

    document.getElementById("placar-txt").innerHTML =
        `Nós ${myTeam === "A" ? ptsA : ptsB} × ${myTeam === "A" ? ptsB : ptsA} Eles`;

// ------------------------------
// 7. INDICAR STATUS / VEZ / MENSAGENS
// ------------------------------
const statusBox = document.getElementById("owner-controls");
statusBox.innerHTML = "";

if (s.status === "waiting") {

    const isOwner = String(s.owner) === String(USER);
    let btns = "";

    if (isOwner) {

        if (players.length < 4) {
            btns += `
                <button class="btn btn-info" onclick="addBot()">
                    🤖 Add Bot
                </button>
            `;
        }

        if (players.length >= 2) {
            btns += `
                <button class="btn btn-success" onclick="startMatch()">
                    INICIAR
                </button>
            `;
        }

        statusBox.innerHTML = btns || "Aguardando jogadores...";
    } 
    else {
        statusBox.innerHTML = `
            <div style="padding:8px 12px;background:rgba(255,255,255,0.1);border-radius:8px;">
                Aguardando o dono da sala iniciar...
            </div>
        `;
    }

} else if (s.status === "resolving_round") {

    statusBox.innerHTML = "Revelando jogadas...";

} else if (s.status === "waiting_truco") {

    const tr = s.truco_request;
    const pv = tr ? Number(tr.valor_proposto || 3) : 3;
    const lbl = trucoLabelForValue(pv);

    if (tr) {
        if (String(tr.target) === String(USER)) {
            statusBox.innerHTML = `PEDIRAM ${lbl}! Você aceita?`;
        } else if (String(tr.author) === String(USER)) {
            statusBox.innerHTML = `Você pediu ${lbl}! Aguardando resposta...`;
        } else {
            statusBox.innerHTML = `Pedido de ${lbl} na mesa...`;
        }
    } else {
        statusBox.innerHTML = "Pedido de TRUCO...";
    }

} else if (s.status === "hand11_decision") {

    statusBox.innerHTML = "<b>MÃO DE 11</b> • decisão na tela";

} else if (s.status === "playing" && s.turn === USER) {

    statusBox.innerHTML = "É a sua vez!";

} else if (s.status === "playing") {

    const alvo = players.find(p => String(p.id) === String(s.turn));
    if (alvo) statusBox.innerHTML = `Vez de ${alvo.name}`;

}



    // ------------------------------
    // 7b. OVERLAY MÃO DE 11 (decisão)
    // - Mostra cartas e botões para o time com 11 pontos
    // ------------------------------
    try {
        const mao11 = s.mao11 || null;
        if (s.status === "hand11_decision" && mao11 && mao11.active && mao11.type === "normal") {
            const myTeamNow = (s.teams || {})[USER] || null;
            const team11 = mao11.team11 || null;

            if (myTeamNow && team11 && String(myTeamNow) === String(team11)) {
                showHand11Overlay(
                    "Seu time está na <b>MÃO DE 11</b>. Você quer jogar ou correr?",
                    `<div class="endgame-actions">
                        <button class="btn btn-success" onclick="sendAction('hand11_play')">JOGAR</button>
                        <button class="btn btn-outline-light" onclick="sendAction('hand11_run')">CORRER</button>
                    </div>`
                );
            } else {
                showHand11Overlay(
                    "Mão de 11: aguardando a decisão do adversário...",
                    `<div class="endgame-actions">
                        <button class="btn btn-secondary" disabled>AGUARDANDO</button>
                    </div>`
                );
            }
        } else {
            hideHand11Overlay();
        }
    } catch(e) {
        try { hideHand11Overlay(); } catch(x) {}
    }

    // ------------------------------
    // 8. BOTÃO TRUCO (APENAS QUANDO PODE)
    // ------------------------------
    const trucoBox = document.getElementById("my-action-btns");
    trucoBox.innerHTML = "";

    // ----- TRUCO PENDENTE (aceitar/correr) -----
    const tr = s.truco_request;
    if (s.status === "waiting_truco" && tr) {
        const rid = tr.rid || JSON.stringify(tr);
        const isAuthor = String(tr.author) === String(USER);

        // guarda o valor proposto atual (pra contra-aumento)
        const proposedVal = Number(tr.valor_proposto || 3);
        window.__TRUCO_PENDING_VALOR_PROPOSTO = proposedVal;

        const lbl = trucoLabelForValue(proposedVal) + "!";
        const myTeamNow = (s.teams || {})[USER];
        const targetTeam = (s.teams || {})[String(tr.target)];
        const canRespond = (!isAuthor && myTeamNow && targetTeam && String(myTeamNow) === String(targetTeam));

        // evita repetir efeitos
        if (rid !== LAST_TRUCO_RID) {
            LAST_TRUCO_RID = rid;
            if (!isAuthor) playTrucoSound(lbl);
        }

        if (canRespond) {
            showTrucoOverlay(lbl, 'Você aceita?', 0);

            // botão de contra-aumento (SEIS/NOVE/DOZE)
            const nextVal = nextTrucoValue(proposedVal);
            const canCounter = (!s.truco_disabled && Number(nextVal) > Number(proposedVal) && Number(proposedVal) < 12);

            trucoBox.innerHTML = `
                <button onclick="sendAction('accept')"
                        class="btn btn-success fw-bold"
                        style="padding:10px 18px; font-size:16px; border-radius:10px;">
                    ACEITAR
                </button>
                ${canCounter ? `
                <button onclick="sendAction('raise')"
                        class="btn btn-danger fw-bold"
                        style="padding:10px 18px; font-size:16px; border-radius:10px;">
                    ${trucoLabelForValue(nextVal)}
                </button>` : ``}
                <button onclick="sendAction('run')"
                        class="btn btn-outline-light fw-bold"
                        style="padding:10px 18px; font-size:16px; border-radius:10px;">
                    CORRER
                </button>
            `;
        } else if (isAuthor && String(tr.target) === 'bot_jarvis') {
            showTrucoOverlay(lbl, 'Jarvis está pensando...', 0);
        }

        // não mostra botão TRUCO enquanto aguarda
    } else {
        // se eu pedi truco e já veio resposta (aceitou ou correu)
        if (TRUCO_WAITING_ACCEPT && s.status !== 'waiting_truco' && !s.truco_request) {
            const accepted = (s.status === 'playing' && TRUCO_LAST_PROPOSED && Number(s.valor_mao || 1) === Number(TRUCO_LAST_PROPOSED));
            if (accepted) showTrucoOverlay('ACEITO!', 'Vamos!', 900);
            else showTrucoOverlay('NÃO!', 'Ele correu!', 900);

            TRUCO_WAITING_ACCEPT = false;
            TRUCO_LAST_PROPOSED = null;
        }

        // evita overlay preso quando não há mais pedido
        if (s.status !== 'waiting_truco' && !TRUCO_OVERLAY_TIMER) {
            hideTrucoOverlay();
        }
    }

    if (s.turn === USER && s.status === "playing" && Number(s.valor_mao || 1) < 12 && !s.truco_disabled) {
        const myTNow = (s.teams || {})[USER];
        const allowedT = s.truco_can_raise_team;

        // se allowedT vier preenchido, só esse time pode pedir o próximo aumento
        const canRaise = (!allowedT) || (myTNow && String(myTNow) === String(allowedT));

        if (canRaise) {
            const nextVal = nextTrucoValue(Number(s.valor_mao || 1));
            const act = (Number(nextVal) === 3 ? "truco" : "raise");
            trucoBox.innerHTML = `
                <button onclick="sendAction('${act}')" 
                        class="btn btn-danger fw-bold"
                        style="padding:10px 18px; font-size:16px; border-radius:10px;">
                    ${trucoLabelForValue(nextVal)}
                </button>
            `;
        }
    }

    // ------------------------------
    // 9. PONTOS DA RODADA (3 BOLINHAS)
    // ------------------------------
    const dots = document.getElementById("round-dots-area");
    dots.innerHTML = "";

    const myT = myTeam;
    for (let i = 0; i < 3; i++) {
        const dot = document.createElement("div");
        dot.className = "dot";

        const h = s.round_history[i];
        if (h) {
            if (h === "D") dot.classList.add("draw");
            else if (h === myT) dot.classList.add("win");
            else dot.classList.add("lose");
        }

        dots.appendChild(dot);
    }

    // ------------------------------
    // 10. SONS (curtos e sem repetir)
    // ------------------------------
    if ((s.table_cards || []).length > 0) playSoundElem("snd-flip");

    // derrota por levar a rodada (atualiza quando round_history cresce)
    const rh = (s.round_history || []);
    // não toca som aqui para não "spam" a cada bolinha; o som fica para o fim da mão/partida
    LAST_RH_LEN = rh.length;
    // fim da mão (winner existe só quando finaliza a mão)
    const handWinner = (s.last_winner_data && s.last_winner_data.winner) ? String(s.last_winner_data.winner) : null;
    if (handWinner && handWinner !== LAST_HAND_WINNER) {
        LAST_HAND_WINNER = handWinner;
        const wTeam = (s.teams && s.teams[handWinner]) ? s.teams[handWinner] : null;
        if (wTeam && myTeam && wTeam === myTeam) playSoundElem("snd-win", 650);
        else playDefeatSfx();
    }

    // ------------------------------
    // 11. FIM DO JOGO (overlay grande)
    // ------------------------------
    const ended = (String(s.status) === 'finished') && (ptsA >= 12 || ptsB >= 12);
    if (ended) {
        const winnerTeam = (ptsA >= 12 && ptsB < 12) ? 'A' : ((ptsB >= 12 && ptsA < 12) ? 'B' : (ptsA >= ptsB ? 'A' : 'B'));
        const win = (myTeam && winnerTeam === myTeam);
        // mostra placar no formato do meu time vs deles (como aparece no placar)
        const myPts = (myTeam === 'A') ? ptsA : ptsB;
        const opPts = (myTeam === 'A') ? ptsB : ptsA;
        showEndgameOverlay(win, myPts, opPts);
    } else {
        hideEndgameOverlay();
    }
}


    </script>
    """

    # preenche placeholders
    html_filled = html_template.replace("__LOBBY_HTML__", lobby_html)
    html_filled = html_filled.replace("__SND_DEAL__", SND_DEAL)
    html_filled = html_filled.replace("__SND_FLIP__", SND_FLIP)
    html_filled = html_filled.replace("__SND_WIN__", SND_WIN)
    html_filled = html_filled.replace("__SND_TRUCO__", SND_TRUCO)
    html_filled = html_filled.replace("__USER_ID__", str(user['id']))
    html_filled = html_filled.replace("__USERNAME__", str(user.get('username','Jogador')))

    try:
        return _get_base_html(html_filled, "")
    except Exception:
        return html_filled




# -------------------------
# RESTART MATCH (mesma mesa, nova partida)
# -------------------------
@truco_bp.route('/api/truco/restart', methods=['POST'])
def api_truco_restart():
    user = session.get('user')
    if not user:
        return jsonify({'ok': False, 'err': 'Não logado.'}), 401

    data = request.get_json(force=True)
    name = data.get('table')
    t = TRUCO_TABLES.get(name)
    if not t:
        return jsonify({'ok': False, 'err': 'Mesa não encontrada.'})

    if str(t.get('owner')) != str(user['id']):
        return jsonify({'ok': False, 'err': 'Apenas o dono pode reiniciar.'})

    # reset do estado da partida
    t['status'] = 'waiting'
    t['deck'] = []
    t['vira'] = None
    t['table_cards'] = []
    t['turn'] = None
    t['valor_mao'] = 1
    t['rodada_atual'] = 1
    t['truco_request'] = None
    t['last_winner_data'] = None
    t['round_history'] = []
    t['team_round_wins'] = {'A': 0, 'B': 0}
    t['_ia_busy'] = False

    for pid, p in (t.get('players') or {}).items():
        p['hand'] = []
        p['round_wins'] = 0
        p['points'] = 0

    # nova partida: precisa debitar de novo quando iniciar
    t['_buyin_collected'] = False
    t['_paid_out'] = False
    t['_match_players'] = list((t.get('players') or {}).keys())

    return jsonify({'ok': True})
# -------------------------
# LIST ROUTE (DEBUG)
# -------------------------
@truco_bp.route('/api/truco/list')
def api_truco_list():
    lista = []
    for nome, t in TRUCO_TABLES.items():
        players = [{'name': p.get('name'), 'avatar': p.get('avatar')} for p in t.get('players', {}).values()]
        lista.append({'name': nome, 'status': t.get('status'), 'bet': t.get('bet'), 'players': players, 'max_players': 4})
    return jsonify(lista)

