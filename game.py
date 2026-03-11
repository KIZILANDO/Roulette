"""
TikTok Guess Game — Jeu multijoueur : devinez qui a liké la vidéo !
Chaque joueur upload son fichier de TikToks likés (.txt).
Un TikTok s'affiche, les joueurs votent pour deviner qui l'a liké.

pip install flask flask-socketio requests
python game.py
"""

import os
import random
import re
import string
import threading
from flask import Flask, request, Response
from flask_socketio import SocketIO, emit, join_room as sio_join_room
import requests as http_requests

app = Flask(__name__)
app.secret_key = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=16 * 1024 * 1024)


# --- Extraction vidéo TikTok via tikwm ---

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

video_cache = {}
cache_lock = threading.Lock()
video_bytes_cache = {}
bytes_cache_lock = threading.Lock()


def extract_video_info(tiktok_url):
    """Extrait l'URL vidéo (pas audio) via tikwm — POST + fallback GET."""
    # Méthode 1 : tikwm POST (plus fiable que GET)
    try:
        r = http_requests.post(
            "https://www.tikwm.com/api/",
            data={"url": tiktok_url, "hd": "1"},
            headers={
                "User-Agent": UA,
                "Referer": "https://www.tikwm.com/",
                "Origin": "https://www.tikwm.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
        data = r.json()
        if data.get("code") == 0:
            d = data["data"]
            # "play" = vidéo sans watermark, "hdplay" = HD
            # NE PAS utiliser "music" / "music_info" qui est juste l'audio
            video_url = d.get("hdplay") or d.get("play")
            if video_url:
                # tikwm renvoie parfois des chemins relatifs
                if video_url.startswith("/"):
                    video_url = "https://www.tikwm.com" + video_url
                print(f"  [tikwm POST] OK → {video_url[:80]}...")
                return {"url": video_url, "headers": {"User-Agent": UA, "Referer": "https://www.tikwm.com/"}}
            else:
                print(f"  [tikwm POST] pas de champ play/hdplay")
    except Exception as e:
        print(f"  [tikwm POST] erreur: {e}")

    # Méthode 2 : tikwm GET (fallback)
    try:
        r = http_requests.get(
            "https://www.tikwm.com/api/",
            params={"url": tiktok_url, "hd": "1"},
            headers={"User-Agent": UA, "Referer": "https://www.tikwm.com/"},
            timeout=15,
        )
        data = r.json()
        if data.get("code") == 0:
            d = data["data"]
            video_url = d.get("hdplay") or d.get("play")
            if video_url:
                if video_url.startswith("/"):
                    video_url = "https://www.tikwm.com" + video_url
                print(f"  [tikwm GET] OK → {video_url[:80]}...")
                return {"url": video_url, "headers": {"User-Agent": UA, "Referer": "https://www.tikwm.com/"}}
    except Exception as e:
        print(f"  [tikwm GET] erreur: {e}")

    print(f"  [extract] ECHEC pour {tiktok_url}")
    return None


def parse_urls(text):
    videos = []
    for line in text.splitlines():
        url = line.strip()
        if not url:
            continue
        match = re.search(r"@([^/]+)/video/(\d+)", url)
        if match:
            videos.append({"url": url, "author": match.group(1), "video_id": match.group(2)})
    return videos


def generate_room_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


# --- État des rooms ---
# room_code -> {
#   host_sid, players: {sid: {pseudo, videos:[], ready}},
#   num_rounds, current_round, phase,
#   used_videos: set(), current_video, current_owner,
#   votes: {sid: voted_pseudo}, scores: {pseudo: int},
#   player_order: [pseudo]
# }
rooms = {}
sid_to_room = {}


# --- Routes HTTP ---

@app.route("/")
def index():
    return HTML_PAGE


@app.route("/test_extract")
def test_extract():
    """Route de debug : teste tikwm avec une URL TikTok connue."""
    test_url = request.args.get("url", "https://www.tiktok.com/@tiktok/video/7106594312292453678")
    info = extract_video_info(test_url)
    if info:
        return f"OK — URL vidéo extraite: {info['url'][:100]}..."
    return "ECHEC — tikwm n'a pas pu extraire la vidéo", 500


def _ensure_bytes_cached(video_id):
    """S'assure que les bytes de la vidéo sont en cache. Retourne le cache entry ou None."""
    with bytes_cache_lock:
        cached = video_bytes_cache.get(video_id)
    if cached:
        return cached

    # Pas en cache bytes, essayer de télécharger depuis video_cache (URL info)
    with cache_lock:
        info = video_cache.get(video_id)
    if not info:
        return None

    dl_headers = {k: v for k, v in info["headers"].items()
                  if k.lower() in ("user-agent", "referer", "cookie", "accept")}
    try:
        resp = http_requests.get(info["url"], headers=dl_headers, timeout=30)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "video/mp4")
        entry = {"bytes": resp.content, "content_type": ct}
        with bytes_cache_lock:
            video_bytes_cache[video_id] = entry
        return entry
    except Exception as e:
        print(f"  [stream] Erreur téléchargement: {e}")
        return None


@app.route("/stream/<video_id>")
def stream_video(video_id):
    """Sert la vidéo depuis le cache avec support des Range requests (HTTP 206)."""
    cached = _ensure_bytes_cached(video_id)
    if not cached:
        return "Vidéo non trouvée", 404

    data = cached["bytes"]
    content_type = cached["content_type"]
    total = len(data)

    # Gérer les Range requests (indispensable pour <video> dans la plupart des navigateurs)
    range_header = request.headers.get("Range")
    if range_header:
        # Parse "bytes=start-end"
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else total - 1
            end = min(end, total - 1)
            chunk = data[start:end + 1]
            return Response(
                chunk,
                status=206,
                content_type=content_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(chunk)),
                    "Cache-Control": "public, max-age=3600",
                },
            )

    return Response(
        data,
        content_type=content_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(total),
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.route("/api/extract")
def api_extract():
    """Extrait et pré-télécharge la vidéo pour la servir via /stream/."""
    url = request.args.get("url", "")
    video_id = request.args.get("video_id", "")
    if not url:
        return {"error": "URL manquante"}, 400
    info = extract_video_info(url)
    if not info:
        return {"error": "Extraction échouée"}, 500
    # Mettre en cache pour que /stream/ puisse servir
    if video_id:
        with cache_lock:
            video_cache[video_id] = info
        _ensure_bytes_cached(video_id)
    return {"video_url": info["url"]}


# --- Socket.IO ---

def room_player_list(room):
    return [
        {"pseudo": p["pseudo"], "ready": p["ready"], "video_count": len(p["videos"])}
        for p in room["players"].values()
    ]


def send_room_update(room_code):
    room = rooms[room_code]
    data = {
        "players": room_player_list(room),
        "host": rooms[room_code]["players"].get(room["host_sid"], {}).get("pseudo", ""),
        "room_code": room_code,
        "num_rounds": room.get("num_rounds", 5),
    }
    emit("room_update", data, to=room_code)


@socketio.on("create_room")
def on_create_room(data):
    pseudo = str(data.get("pseudo", "")).strip()[:20]
    if not pseudo:
        emit("error", {"msg": "Pseudo requis"})
        return
    code = generate_room_code()
    while code in rooms:
        code = generate_room_code()

    rooms[code] = {
        "host_sid": request.sid,
        "players": {
            request.sid: {"pseudo": pseudo, "videos": [], "ready": False}
        },
        "num_rounds": 5,
        "current_round": 0,
        "phase": "lobby",
        "used_videos": set(),
        "current_video": None,
        "current_owner": None,
        "votes": {},
        "scores": {},
        "player_order": [pseudo],
    }
    sid_to_room[request.sid] = code
    sio_join_room(code)
    emit("room_created", {"room_code": code, "pseudo": pseudo})
    send_room_update(code)


@socketio.on("join_game")
def on_join_game(data):
    pseudo = str(data.get("pseudo", "")).strip()[:20]
    code = str(data.get("room_code", "")).strip().upper()
    if not pseudo:
        emit("error", {"msg": "Pseudo requis"})
        return
    if code not in rooms:
        emit("error", {"msg": "Room introuvable"})
        return
    room = rooms[code]
    if room["phase"] != "lobby":
        emit("error", {"msg": "Partie déjà en cours"})
        return
    # Check duplicate pseudo
    for p in room["players"].values():
        if p["pseudo"].lower() == pseudo.lower():
            emit("error", {"msg": "Ce pseudo est déjà pris"})
            return

    room["players"][request.sid] = {"pseudo": pseudo, "videos": [], "ready": False}
    room["player_order"].append(pseudo)
    sid_to_room[request.sid] = code
    sio_join_room(code)
    emit("room_joined", {"room_code": code, "pseudo": pseudo})
    send_room_update(code)


@socketio.on("upload_file")
def on_upload_file(data):
    code = sid_to_room.get(request.sid)
    if not code or code not in rooms:
        return
    room = rooms[code]
    player = room["players"].get(request.sid)
    if not player:
        return
    content = str(data.get("content", ""))
    videos = parse_urls(content)
    if not videos:
        emit("error", {"msg": "Aucune URL TikTok valide trouvée dans le fichier"})
        return
    player["videos"] = videos
    player["ready"] = True
    emit("file_accepted", {"count": len(videos)})
    send_room_update(code)


@socketio.on("set_rounds")
def on_set_rounds(data):
    code = sid_to_room.get(request.sid)
    if not code or code not in rooms:
        return
    room = rooms[code]
    if room["host_sid"] != request.sid:
        return
    n = int(data.get("num_rounds", 5))
    room["num_rounds"] = max(1, min(n, 50))
    send_room_update(code)


@socketio.on("start_game")
def on_start_game(_data=None):
    code = sid_to_room.get(request.sid)
    if not code or code not in rooms:
        return
    room = rooms[code]
    if room["host_sid"] != request.sid:
        emit("error", {"msg": "Seul l'hôte peut lancer la partie"})
        return
    if len(room["players"]) < 2:
        emit("error", {"msg": "Il faut au moins 2 joueurs"})
        return
    # Check all ready
    for p in room["players"].values():
        if not p["ready"]:
            emit("error", {"msg": f"{p['pseudo']} n'a pas encore uploadé son fichier"})
            return

    # Init scores
    for p in room["players"].values():
        room["scores"][p["pseudo"]] = 0
    room["current_round"] = 0
    room["phase"] = "playing"
    room["used_videos"] = set()
    emit("game_started", {"num_rounds": room["num_rounds"]}, to=code)
    start_round(code)


def start_round(room_code):
    room = rooms[room_code]
    room["current_round"] += 1
    room["votes"] = {}
    room["phase"] = "viewing"

    # Pick random player & random video not yet used
    eligible = [
        (sid, p) for sid, p in room["players"].items()
        if any(v["video_id"] not in room["used_videos"] for v in p["videos"])
    ]
    if not eligible:
        # Reset used videos if we've exhausted them
        room["used_videos"] = set()
        eligible = [(sid, p) for sid, p in room["players"].items() if p["videos"]]

    if not eligible:
        end_game(room_code)
        return

    owner_sid, owner = random.choice(eligible)
    available = [v for v in owner["videos"] if v["video_id"] not in room["used_videos"]]
    video = random.choice(available)
    room["used_videos"].add(video["video_id"])
    room["current_video"] = video
    room["current_owner"] = owner["pseudo"]

    # Extraire et pré-télécharger la vidéo côté serveur
    video_ready = False
    info = extract_video_info(video["url"])
    if info and info["url"]:
        with cache_lock:
            video_cache[video["video_id"]] = info
        # Pré-télécharger les bytes pour servir tous les joueurs via /stream/
        try:
            dl_headers = {k: v for k, v in info["headers"].items()
                         if k.lower() in ("user-agent", "referer", "cookie", "accept")}
            resp = http_requests.get(info["url"], headers=dl_headers, timeout=30)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "video/mp4")
            with bytes_cache_lock:
                # Garder seulement la vidéo du round en cours pour économiser la RAM
                video_bytes_cache.clear()
                video_bytes_cache[video["video_id"]] = {"bytes": resp.content, "content_type": ct}
            video_ready = True
            print(f"  ✓ Vidéo {video['video_id']} pré-téléchargée ({len(resp.content) // 1024} KB)")
        except Exception as e:
            print(f"  ✗ Pré-téléchargement échoué: {e}")
    else:
        print(f"  ✗ Extraction serveur échouée, le client fera l'extraction")

    round_data = {
        "round": room["current_round"],
        "total_rounds": room["num_rounds"],
        "video_ready": video_ready,
        "tiktok_url": video["url"],
        "video_id": video["video_id"],
        "author": video["author"],
        "players": [p["pseudo"] for p in room["players"].values()],
        "scores": room["scores"],
    }

    # Send to each player — the owner gets a flag
    for sid, p in room["players"].items():
        personal_data = {**round_data, "is_yours": (p["pseudo"] == room["current_owner"])}
        emit("new_round", personal_data, to=sid)

    room["phase"] = "voting"


@socketio.on("submit_vote")
def on_submit_vote(data):
    code = sid_to_room.get(request.sid)
    if not code or code not in rooms:
        return
    room = rooms[code]
    if room["phase"] != "voting":
        return
    player = room["players"].get(request.sid)
    if not player:
        return
    # Owner can't vote
    if player["pseudo"] == room["current_owner"]:
        return
    voted = str(data.get("voted_pseudo", "")).strip()
    # Validate vote
    valid_pseudos = [p["pseudo"] for p in room["players"].values()]
    if voted not in valid_pseudos:
        return
    room["votes"][request.sid] = voted

    # Notify vote count
    expected = len(room["players"]) - 1  # owner doesn't vote
    emit("vote_count", {"count": len(room["votes"]), "total": expected}, to=code)

    # All votes in?
    if len(room["votes"]) >= expected:
        reveal_round(code)


def reveal_round(room_code):
    room = rooms[room_code]
    room["phase"] = "reveal"
    owner = room["current_owner"]

    # Tally points
    results = {}
    for sid, voted in room["votes"].items():
        pseudo = room["players"][sid]["pseudo"]
        correct = (voted == owner)
        if correct:
            room["scores"][pseudo] = room["scores"].get(pseudo, 0) + 1
        results[pseudo] = {"voted": voted, "correct": correct}

    reveal_data = {
        "owner": owner,
        "results": results,
        "scores": room["scores"],
        "round": room["current_round"],
        "total_rounds": room["num_rounds"],
    }
    emit("round_reveal", reveal_data, to=room_code)

    # Check if game over
    if room["current_round"] >= room["num_rounds"]:
        end_game(room_code)


@socketio.on("next_round")
def on_next_round(_data=None):
    code = sid_to_room.get(request.sid)
    if not code or code not in rooms:
        return
    room = rooms[code]
    if room["host_sid"] != request.sid:
        return
    if room["phase"] != "reveal":
        return
    if room["current_round"] >= room["num_rounds"]:
        end_game(code)
        return
    start_round(code)


def end_game(room_code):
    room = rooms[room_code]
    room["phase"] = "finished"
    # Sort by score descending
    ranking = sorted(room["scores"].items(), key=lambda x: x[1], reverse=True)
    emit("game_over", {"ranking": ranking, "scores": room["scores"]}, to=room_code)


@socketio.on("disconnect")
def on_disconnect():
    code = sid_to_room.pop(request.sid, None)
    if not code or code not in rooms:
        return
    room = rooms[code]
    player = room["players"].pop(request.sid, None)
    if player:
        if player["pseudo"] in room["player_order"]:
            room["player_order"].remove(player["pseudo"])
    if not room["players"]:
        del rooms[code]
        return
    # If host left, assign new host
    if room["host_sid"] == request.sid:
        new_host_sid = next(iter(room["players"]))
        room["host_sid"] = new_host_sid
    if room["phase"] == "lobby":
        send_room_update(code)
    else:
        emit("player_left", {"pseudo": player["pseudo"] if player else "?"}, to=code)


# --- HTML ---

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TikTok Guess Game</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.4/socket.io.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Inter',sans-serif;background:#0a0a0a;color:#fff;min-height:100vh;display:flex;justify-content:center;align-items:center}
  .page{display:none;width:100%;max-width:500px;padding:20px;text-align:center}
  .page.active{display:block}
  h1{font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,#fe2c55,#25f4ee);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}
  h2{font-size:1.3rem;color:#ccc;margin-bottom:20px}
  .subtitle{color:#888;font-size:.85rem;margin-bottom:25px}
  input[type=text],input[type=number]{width:100%;padding:14px 18px;border-radius:12px;border:2px solid #333;background:#161622;color:#fff;font-size:1rem;margin-bottom:12px;outline:none;transition:border .2s}
  input:focus{border-color:#25f4ee}
  .btn{padding:14px 40px;font-size:1.05rem;font-weight:700;border:none;border-radius:50px;cursor:pointer;color:#fff;transition:all .3s;letter-spacing:.5px;display:inline-block;margin:6px}
  .btn-primary{background:linear-gradient(135deg,#fe2c55,#ff6b81)}
  .btn-primary:hover{transform:scale(1.05);box-shadow:0 0 25px rgba(254,44,85,.5)}
  .btn-secondary{background:linear-gradient(135deg,#25f4ee,#00c9db)}
  .btn-secondary:hover{transform:scale(1.05);box-shadow:0 0 25px rgba(37,244,238,.4)}
  .btn-small{padding:10px 24px;font-size:.9rem}
  .btn:disabled{opacity:.4;pointer-events:none}
  .room-code{font-size:2.5rem;font-weight:800;color:#25f4ee;letter-spacing:8px;margin:15px 0;user-select:all}
  .player-list{list-style:none;margin:15px 0;text-align:left}
  .player-list li{padding:10px 16px;background:#161622;border-radius:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;font-size:.95rem}
  .player-list li .status{font-size:.75rem;padding:4px 10px;border-radius:20px;font-weight:600}
  .status.ready{background:#25f4ee22;color:#25f4ee}
  .status.waiting{background:#fe2c5522;color:#fe2c55}
  .file-upload{border:2px dashed #333;border-radius:16px;padding:30px 20px;margin:15px 0;cursor:pointer;transition:all .3s;position:relative}
  .file-upload:hover{border-color:#25f4ee;background:#25f4ee08}
  .file-upload input{position:absolute;inset:0;opacity:0;cursor:pointer}
  .file-upload .icon{font-size:2rem;margin-bottom:8px}
  .file-upload .label{color:#888;font-size:.85rem}
  .file-upload.done{border-color:#25f4ee;background:#25f4ee0a}
  .file-upload.done .label{color:#25f4ee}
  .error-msg{color:#fe2c55;font-size:.85rem;margin:8px 0;min-height:20px}
  .video-wrapper{width:100%;max-width:400px;margin:0 auto;border-radius:16px;overflow:hidden;background:#111;box-shadow:0 10px 40px rgba(0,0,0,.5)}
  .video-wrapper video{width:100%;max-height:75vh;background:#000;display:block}
  .round-info{margin:15px 0;font-size:.9rem;color:#888}
  .round-badge{display:inline-block;background:#25f4ee22;color:#25f4ee;padding:6px 16px;border-radius:20px;font-weight:700;font-size:.85rem;margin-bottom:10px}
  .owner-alert{background:linear-gradient(135deg,#fe2c5522,#ff6b8122);border:2px solid #fe2c55;border-radius:12px;padding:16px;margin:12px 0;font-weight:600;color:#fe2c55;font-size:.95rem}
  .vote-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:15px 0}
  .vote-btn{padding:14px;border-radius:12px;border:2px solid #333;background:#161622;color:#fff;font-size:.95rem;font-weight:600;cursor:pointer;transition:all .2s}
  .vote-btn:hover{border-color:#25f4ee;background:#25f4ee11}
  .vote-btn.selected{border-color:#25f4ee;background:#25f4ee22;color:#25f4ee}
  .result-card{background:#161622;border-radius:12px;padding:12px 16px;margin:6px 0;display:flex;justify-content:space-between;align-items:center;font-size:.9rem}
  .result-card .correct{color:#25f4ee}
  .result-card .wrong{color:#fe2c55}
  .scoreboard{margin:15px 0}
  .score-row{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:#161622;border-radius:10px;margin:6px 0;font-size:.95rem}
  .score-row .pts{font-weight:800;color:#25f4ee;font-size:1.1rem}
  .ranking-page .medal{font-size:2rem;margin-right:8px}
  .ranking-page .rank-1{background:linear-gradient(135deg,#ffd70033,#ffa50022);border:2px solid #ffd700}
  .ranking-page .rank-2{background:linear-gradient(135deg,#c0c0c033,#a8a8a822);border:2px solid #c0c0c0}
  .ranking-page .rank-3{background:linear-gradient(135deg,#cd7f3233,#a0522d22);border:2px solid #cd7f32}
  .vote-status{color:#888;font-size:.85rem;margin:10px 0}
  .loading-spinner{display:inline-block;width:20px;height:20px;border:3px solid #333;border-top-color:#25f4ee;border-radius:50%;animation:sp .6s linear infinite;margin-right:8px;vertical-align:middle}
  @keyframes sp{to{transform:rotate(360deg)}}
  .settings-row{display:flex;align-items:center;gap:10px;justify-content:center;margin:12px 0}
  .settings-row label{color:#888;font-size:.9rem}
  .settings-row input[type=number]{width:70px;text-align:center;margin:0}
  .confetti{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:999}
</style>
</head>
<body>

<!-- PAGE 0 : Accueil -->
<div class="page active" id="pageHome">
  <h1>🎮 TikTok Guess</h1>
  <p class="subtitle">Qui a liké cette vidéo ? Devinez et gagnez des points !</p>
  <input type="text" id="pseudoInput" placeholder="Ton pseudo" maxlength="20">
  <div class="error-msg" id="homeError"></div>
  <button class="btn btn-primary" onclick="createRoom()">🏠 Créer une partie</button>
  <button class="btn btn-secondary" onclick="showJoin()">🚪 Rejoindre</button>
  <div id="joinSection" style="display:none;margin-top:15px">
    <input type="text" id="roomCodeInput" placeholder="Code de la room" maxlength="5" style="text-transform:uppercase;text-align:center;letter-spacing:4px;font-size:1.2rem">
    <button class="btn btn-secondary btn-small" onclick="joinRoom()">Rejoindre</button>
  </div>
</div>

<!-- PAGE 1 : Lobby -->
<div class="page" id="pageLobby">
  <h2>Lobby</h2>
  <p class="subtitle">Partagez ce code avec vos amis</p>
  <div class="room-code" id="lobbyRoomCode">-----</div>
  <div id="hostSettings" style="display:none">
    <div class="settings-row">
      <label>Nombre de manches :</label>
      <input type="number" id="numRoundsInput" value="5" min="1" max="50" onchange="setRounds()">
    </div>
  </div>
  <div class="file-upload" id="fileUpload">
    <input type="file" accept=".txt" onchange="uploadFile(event)">
    <div class="icon">📁</div>
    <div class="label">Clique ou glisse ton fichier liked_urls.txt ici</div>
  </div>
  <ul class="player-list" id="lobbyPlayers"></ul>
  <div class="error-msg" id="lobbyError"></div>
  <button class="btn btn-primary" id="startBtn" style="display:none" onclick="startGame()">🚀 Lancer la partie</button>
</div>

<!-- PAGE 2 : Jeu (vidéo + vote) -->
<div class="page" id="pageGame">
  <div class="round-badge" id="roundBadge">Manche 1/5</div>
  <div id="ownerAlert" class="owner-alert" style="display:none">
    🤫 C'est TA vidéo ! Fais semblant de ne pas savoir...
  </div>
  <div class="video-wrapper" id="videoWrapper">
    <div style="padding:40px;color:#888"><span class="loading-spinner"></span>Chargement...</div>
  </div>
  <div id="voteSection" style="display:none">
    <p style="margin:15px 0;font-size:.95rem;color:#ccc">Qui a liké cette vidéo ?</p>
    <div class="vote-grid" id="voteGrid"></div>
    <button class="btn btn-primary btn-small" id="confirmVoteBtn" onclick="confirmVote()" disabled>Voter</button>
  </div>
  <div class="vote-status" id="voteStatus"></div>
  <div class="scoreboard" id="gameScoreboard"></div>
</div>

<!-- PAGE 3 : Résultat du round -->
<div class="page" id="pageReveal">
  <div class="round-badge" id="revealRoundBadge">Résultat</div>
  <h2 id="revealOwner"></h2>
  <div id="revealResults"></div>
  <div class="scoreboard" id="revealScoreboard"></div>
  <button class="btn btn-primary" id="nextRoundBtn" style="display:none" onclick="nextRound()">Manche suivante ➡</button>
  <div id="waitNextMsg" style="display:none;color:#888;margin-top:15px"><span class="loading-spinner"></span>L'hôte lance la prochaine manche...</div>
</div>

<!-- PAGE 4 : Classement final -->
<div class="page ranking-page" id="pageRanking">
  <h1>🏆 Classement Final</h1>
  <div id="finalRanking" style="margin:20px 0"></div>
  <button class="btn btn-secondary" onclick="location.reload()">🔄 Nouvelle partie</button>
</div>

<canvas class="confetti" id="confetti"></canvas>

<script>
const socket = io();

let myPseudo = '';
let isHost = false;
let roomCode = '';
let selectedVote = null;
let currentPlayers = [];

// --- Navigation ---
function showPage(id) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// --- Accueil ---
function showJoin() {
  document.getElementById('joinSection').style.display = 'block';
  document.getElementById('roomCodeInput').focus();
}

function createRoom() {
  const pseudo = document.getElementById('pseudoInput').value.trim();
  if (!pseudo) { document.getElementById('homeError').textContent = 'Entre un pseudo !'; return; }
  myPseudo = pseudo;
  isHost = true;
  socket.emit('create_room', { pseudo });
}

function joinRoom() {
  const pseudo = document.getElementById('pseudoInput').value.trim();
  const code = document.getElementById('roomCodeInput').value.trim().toUpperCase();
  if (!pseudo) { document.getElementById('homeError').textContent = 'Entre un pseudo !'; return; }
  if (!code) { document.getElementById('homeError').textContent = 'Entre le code de la room !'; return; }
  myPseudo = pseudo;
  isHost = false;
  socket.emit('join_game', { pseudo, room_code: code });
}

// --- Lobby ---
function uploadFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(ev) {
    socket.emit('upload_file', { content: ev.target.result });
  };
  reader.readAsText(file);
}

function setRounds() {
  const n = parseInt(document.getElementById('numRoundsInput').value) || 5;
  socket.emit('set_rounds', { num_rounds: n });
}

function startGame() {
  socket.emit('start_game', {});
}

// --- Vote ---
function selectVote(pseudo) {
  selectedVote = pseudo;
  document.querySelectorAll('.vote-btn').forEach(b => {
    b.classList.toggle('selected', b.dataset.pseudo === pseudo);
  });
  document.getElementById('confirmVoteBtn').disabled = false;
}

function confirmVote() {
  if (!selectedVote) return;
  socket.emit('submit_vote', { voted_pseudo: selectedVote });
  document.getElementById('confirmVoteBtn').disabled = true;
  document.getElementById('confirmVoteBtn').textContent = 'Vote envoyé ✓';
  document.querySelectorAll('.vote-btn').forEach(b => b.style.pointerEvents = 'none');
}

function nextRound() {
  socket.emit('next_round', {});
}

function renderScoreboard(scores, containerId) {
  const el = document.getElementById(containerId);
  const sorted = Object.entries(scores).sort((a, b) => b[1] - a[1]);
  el.innerHTML = '<h3 style="font-size:.8rem;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Scores</h3>' +
    sorted.map(([p, s]) =>
      `<div class="score-row"><span>${p === myPseudo ? '👤 ' : ''}${p}</span><span class="pts">${s} pt${s > 1 ? 's' : ''}</span></div>`
    ).join('');
}

// --- Chargement vidéo : proxy serveur en priorité ---
async function loadVideo(videoReady, tiktokUrl, videoId, wrapper) {
  // Méthode 1 : Proxy serveur (vidéo pré-téléchargée, fiable pour tous les joueurs)
  console.log('[video] Essai proxy serveur...');
  if (await tryPlayVideo('/stream/' + videoId, wrapper)) return;

  // Méthode 2 : Proxy serveur via extraction à la demande
  console.log('[video] Essai extraction serveur...');
  try {
    const r = await fetch('/api/extract?url=' + encodeURIComponent(tiktokUrl) + '&video_id=' + encodeURIComponent(videoId));
    const data = await r.json();
    if (data.video_url) {
      if (await tryPlayVideo('/stream/' + videoId, wrapper)) return;
    }
  } catch(e) {
    console.log('[video] extract erreur:', e.message);
  }

  // Méthode 3 : Extraction côté client via tikwm (dernier recours)
  console.log('[video] Essai tikwm côté client...');
  try {
    const r = await fetch('https://www.tikwm.com/api/', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'url=' + encodeURIComponent(tiktokUrl) + '&hd=1',
    });
    const data = await r.json();
    if (data.code === 0) {
      let vurl = data.data.hdplay || data.data.play;
      if (vurl) {
        if (vurl.startsWith('/')) vurl = 'https://www.tikwm.com' + vurl;
        console.log('[video] tikwm client OK');
        if (await tryPlayVideo(vurl, wrapper)) return;
      }
    }
  } catch(e) {
    console.log('[video] tikwm client CORS/erreur:', e.message);
  }

  wrapper.innerHTML = '<div style="padding:30px;color:#fe2c55">Impossible de charger la vidéo 😕</div>';
}

function tryPlayVideo(url, wrapper) {
  return new Promise(resolve => {
    const video = document.createElement('video');
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.muted = true;
    video.style.cssText = 'width:100%;max-height:75vh;background:#000;display:block';

    const timeout = setTimeout(() => {
      console.log('[video] Timeout pour', url.substring(0, 60));
      video.src = '';
      resolve(false);
    }, 20000);

    video.oncanplay = () => {
      clearTimeout(timeout);
      wrapper.innerHTML = '';
      wrapper.appendChild(video);
      video.muted = false;
      video.play().catch(() => { video.muted = true; video.play().catch(() => {}); });
      resolve(true);
    };

    video.onerror = () => {
      clearTimeout(timeout);
      console.log('[video] Erreur pour', url.substring(0, 60));
      video.src = '';
      resolve(false);
    };

    video.src = url;
  });
}

// --- Socket events ---

socket.on('room_created', data => {
  roomCode = data.room_code;
  document.getElementById('lobbyRoomCode').textContent = roomCode;
  document.getElementById('hostSettings').style.display = 'block';
  showPage('pageLobby');
});

socket.on('room_joined', data => {
  roomCode = data.room_code;
  document.getElementById('lobbyRoomCode').textContent = roomCode;
  showPage('pageLobby');
});

socket.on('room_update', data => {
  const list = document.getElementById('lobbyPlayers');
  list.innerHTML = data.players.map(p =>
    `<li><span>${p.pseudo === myPseudo ? '👤 ' : ''}${p.pseudo}</span>
     <span class="status ${p.ready ? 'ready' : 'waiting'}">${p.ready ? '✓ ' + p.video_count + ' vidéos' : 'En attente...'}</span></li>`
  ).join('');
  document.getElementById('numRoundsInput').value = data.num_rounds;

  // Show start button for host if >= 2 players and all ready
  const allReady = data.players.every(p => p.ready);
  const startBtn = document.getElementById('startBtn');
  if (isHost && data.players.length >= 2 && allReady) {
    startBtn.style.display = 'inline-block';
  } else {
    startBtn.style.display = 'none';
  }
});

socket.on('file_accepted', data => {
  const fu = document.getElementById('fileUpload');
  fu.classList.add('done');
  fu.querySelector('.icon').textContent = '✅';
  fu.querySelector('.label').textContent = data.count + ' vidéos chargées !';
});

socket.on('error', data => {
  // Show error on visible page
  const errEls = ['homeError', 'lobbyError'];
  errEls.forEach(id => { document.getElementById(id).textContent = data.msg; });
  setTimeout(() => errEls.forEach(id => { document.getElementById(id).textContent = ''; }), 4000);
});

socket.on('game_started', data => {
  showPage('pageGame');
});

socket.on('new_round', data => {
  showPage('pageGame');
  selectedVote = null;
  currentPlayers = data.players;
  document.getElementById('roundBadge').textContent = 'Manche ' + data.round + '/' + data.total_rounds;

  // Owner alert
  const ownerAlert = document.getElementById('ownerAlert');
  ownerAlert.style.display = data.is_yours ? 'block' : 'none';

  // Video — 3 méthodes en cascade
  const wrapper = document.getElementById('videoWrapper');
  wrapper.innerHTML = '<div style="padding:40px;color:#888"><span class="loading-spinner"></span>Chargement de la vidéo...</div>';
  loadVideo(data.video_ready, data.tiktok_url, data.video_id, wrapper);

  // Vote section
  const voteSection = document.getElementById('voteSection');
  const voteGrid = document.getElementById('voteGrid');
  if (data.is_yours) {
    voteSection.style.display = 'none';
  } else {
    voteSection.style.display = 'block';
    voteGrid.innerHTML = data.players.map(p =>
      `<button class="vote-btn" data-pseudo="${p}" onclick="selectVote('${p}')">${p}</button>`
    ).join('');
    document.getElementById('confirmVoteBtn').disabled = true;
    document.getElementById('confirmVoteBtn').textContent = 'Voter';
  }

  document.getElementById('voteStatus').textContent = '';
  renderScoreboard(data.scores, 'gameScoreboard');
});

socket.on('vote_count', data => {
  document.getElementById('voteStatus').textContent = data.count + '/' + data.total + ' votes reçus';
});

socket.on('round_reveal', data => {
  showPage('pageReveal');
  document.getElementById('revealRoundBadge').textContent = 'Manche ' + data.round + '/' + data.total_rounds;
  document.getElementById('revealOwner').innerHTML = 'C\'était <span style="color:#25f4ee">' + data.owner + '</span> !';

  const resultsEl = document.getElementById('revealResults');
  resultsEl.innerHTML = Object.entries(data.results).map(([pseudo, r]) =>
    `<div class="result-card">
      <span>${pseudo} a voté <b>${r.voted}</b></span>
      <span class="${r.correct ? 'correct' : 'wrong'}">${r.correct ? '✓ +1' : '✗'}</span>
    </div>`
  ).join('');

  renderScoreboard(data.scores, 'revealScoreboard');

  // Show next round button for host
  if (isHost && data.round < data.total_rounds) {
    document.getElementById('nextRoundBtn').style.display = 'inline-block';
    document.getElementById('waitNextMsg').style.display = 'none';
  } else if (!isHost && data.round < data.total_rounds) {
    document.getElementById('nextRoundBtn').style.display = 'none';
    document.getElementById('waitNextMsg').style.display = 'block';
  } else {
    document.getElementById('nextRoundBtn').style.display = 'none';
    document.getElementById('waitNextMsg').style.display = 'none';
  }
});

socket.on('game_over', data => {
  showPage('pageRanking');
  const medals = ['🥇', '🥈', '🥉'];
  const rankEl = document.getElementById('finalRanking');
  rankEl.innerHTML = data.ranking.map(([pseudo, score], i) => {
    const cls = i < 3 ? ' rank-' + (i + 1) : '';
    const medal = medals[i] || '';
    return `<div class="score-row${cls}" style="font-size:1.1rem;padding:16px 20px;margin:8px 0">
      <span>${medal ? '<span class="medal">' + medal + '</span>' : (i+1) + '. '}${pseudo}</span>
      <span class="pts">${score} pt${score > 1 ? 's' : ''}</span>
    </div>`;
  }).join('');
  launchConfetti();
});

socket.on('player_left', data => {
  // Simple notification — could be improved
  console.log(data.pseudo + ' a quitté la partie');
});

// --- Confetti ---
function launchConfetti() {
  const canvas = document.getElementById('confetti');
  const ctx = canvas.getContext('2d');
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
  const pieces = [];
  const colors = ['#fe2c55', '#25f4ee', '#ffd700', '#ff6b81', '#00c9db', '#fff'];
  for (let i = 0; i < 150; i++) {
    pieces.push({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height - canvas.height,
      w: Math.random() * 10 + 5,
      h: Math.random() * 6 + 3,
      color: colors[Math.floor(Math.random() * colors.length)],
      vy: Math.random() * 3 + 2,
      vx: (Math.random() - 0.5) * 2,
      rot: Math.random() * 360,
      vr: (Math.random() - 0.5) * 10,
    });
  }
  let frame = 0;
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    pieces.forEach(p => {
      p.y += p.vy;
      p.x += p.vx;
      p.rot += p.vr;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot * Math.PI / 180);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    });
    frame++;
    if (frame < 200) requestAnimationFrame(draw);
    else ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
  draw();
}

// Enter key support
document.getElementById('pseudoInput').addEventListener('keyup', e => {
  if (e.key === 'Enter') {
    if (document.getElementById('joinSection').style.display !== 'none') joinRoom();
    else createRoom();
  }
});
document.getElementById('roomCodeInput').addEventListener('keyup', e => {
  if (e.key === 'Enter') joinRoom();
});
</script>
</body>
</html>"""


# --- Lancement ---

if __name__ == "__main__":
    print("\n🎮 TikTok Guess Game — http://localhost:5000")
    print("   Ctrl+C pour arrêter\n")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
