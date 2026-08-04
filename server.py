import hmac
import json
import os
import time
import threading
import unicodedata
from datetime import date, datetime
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from rapidfuzz import fuzz

load_dotenv()

app = Flask(__name__)
CORS(app)

API_BASE = "https://api.i9logic.net/v1"
CLIENT_ID = os.environ["I9LOGIC_CLIENT_ID"]
TOKEN = os.environ["I9LOGIC_TOKEN"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
if len(ADMIN_PASSWORD) < 8:
    raise RuntimeError("ADMIN_PASSWORD ausente ou muito curta (minimo 8 caracteres).")


def _admin_password_ok(candidate):
    return hmac.compare_digest(str(candidate or "").encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))


ADMIN_LOGIN_MAX_ATTEMPTS = 10
ADMIN_LOGIN_WINDOW_SECONDS = 300
_admin_login_attempts = {}
_bipador_login_attempts = {}
# Hash "dummy" comparado quando o usuario/senha nao existe, para o login de
# bipador levar sempre o mesmo tempo (evita descobrir contas validas por
# temporizacao mesmo com a mensagem de erro generica).
_DUMMY_PASSWORD_HASH = generate_password_hash("tempo-constante-nao-e-uma-senha-real")


def _rate_limited(bucket, key):
    now = time.time()
    count, window_start = bucket.get(key, (0, now))
    if now - window_start > ADMIN_LOGIN_WINDOW_SECONDS:
        count, window_start = 0, now
    return count >= ADMIN_LOGIN_MAX_ATTEMPTS, count, window_start


def _record_failed_attempt(bucket, key, count, window_start):
    bucket[key] = (count + 1, window_start)


DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
CACHE_FILE = os.path.join(DATA_DIR, "cache_data.json")
AUDIT_FILE = os.path.join(DATA_DIR, "audit_sessions.json")
DEDUP_FILE = os.path.join(DATA_DIR, "dedup_groups.json")
SALES_CACHE_FILE = os.path.join(DATA_DIR, "sales_cache.json")
SEED_CACHE_FILE = os.path.join(APP_DIR, "cache_data.json")  # bundled with Docker image
_audit_lock = threading.Lock()
_users_lock = threading.Lock()
_dedup_lock = threading.Lock()
_sales_cache_lock = threading.Lock()

HEADERS = {
    "X-Client-Id": CLIENT_ID,
    "Authorization": f"Bearer {TOKEN}",
}

_last_request_time = 0
_MIN_INTERVAL = 3.0


def _rate_limit():
    global _last_request_time
    now = time.time()
    wait = _last_request_time + _MIN_INTERVAL - now
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.time()


# ponytail: server-side cache, single source for all users
CACHE = {"filiais": [], "produtos": [], "estoques": [], "precos": [], "ready": False, "loading": False, "progress": {}, "last_error": None}


def _paginated_fetch(entity, filter_fn=None, per_page=200):
    all_data = []
    page = 1
    while True:
        for attempt in range(10):
            _rate_limit()
            resp = requests.get(f"{API_BASE}/{entity}", headers=HEADERS,
                              params={"page": page, "per_page": per_page}, timeout=45)
            body = resp.json()
            if resp.status_code == 429 or (not body.get("ok") and "RATE_LIMIT" in str(body.get("error", ""))):
                wait = min(30 * (2 ** attempt), 120)
                print(f"[cache] rate-limit em {entity} p{page}, tentativa {attempt+1}/10, aguardando {wait}s...")
                time.sleep(wait)
                continue
            break
        if not body.get("ok"):
            raise RuntimeError(f"Falha ao buscar '{entity}' pagina {page}: {body.get('error') or resp.status_code}")
        data = body.get("data", [])
        if filter_fn:
            data = [d for d in data if filter_fn(d)]
        all_data.extend(data)
        total = body.get("total", 0)
        if len(all_data) >= total or len(data) == 0:
            break
        page += 1
    return all_data


def refresh_cache():
    """So confirma (ready + grava em disco) apos as 4 entidades sincronizarem por completo.
    Uma falha parcial (ex: rate limit) mantem o cache anterior intacto em vez de gravar dados incompletos.
    'progress' vai sendo preenchido entidade-a-entidade so para a barra de progresso do front."""
    print("[cache] Iniciando sincronizacao com a API...")
    CACHE["loading"] = True
    CACHE["progress"] = {}
    try:
        print("[cache] Baixando filiais...")
        filiais = _paginated_fetch("filiais", per_page=100)
        CACHE["progress"]["filiais"] = len(filiais)
        print(f"[cache] {len(filiais)} filiais")
        print("[cache] Baixando produtos...")
        produtos = _paginated_fetch("produtos", filter_fn=lambda p: p.get("ean") and str(p["ean"]).strip())
        CACHE["progress"]["produtos"] = len(produtos)
        print(f"[cache] {len(produtos)} produtos")
        print("[cache] Baixando estoques...")
        estoques = _paginated_fetch("produtos_estoques", per_page=500)
        CACHE["progress"]["estoques"] = len(estoques)
        print(f"[cache] {len(estoques)} estoques")
        print("[cache] Baixando precos...")
        precos = _paginated_fetch("precos", per_page=500)
        CACHE["progress"]["precos"] = len(precos)
        print(f"[cache] {len(precos)} precos")

        CACHE["filiais"] = filiais
        CACHE["produtos"] = produtos
        CACHE["estoques"] = estoques
        CACHE["precos"] = precos
        CACHE["ready"] = True
        CACHE["synced_at"] = datetime.now().isoformat()
        _save_cache_to_disk()
        print(f"[cache] Sincronizacao concluida: {len(produtos)} produtos, {len(estoques)} estoques, {len(precos)} precos")
        CACHE["last_error"] = None
    except Exception as exc:
        CACHE["last_error"] = str(exc)
        print(f"[cache] Falha ao sincronizar: {exc}")
    finally:
        CACHE["loading"] = False
        CACHE["progress"] = {}


def _save_cache_to_disk():
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "filiais": CACHE["filiais"], "produtos": CACHE["produtos"],
            "estoques": CACHE["estoques"], "precos": CACHE["precos"],
            "synced_at": CACHE.get("synced_at"),
        }, f, ensure_ascii=False)


def _load_cache_from_disk():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    CACHE["filiais"] = data.get("filiais", [])
    CACHE["produtos"] = data.get("produtos", [])
    CACHE["estoques"] = data.get("estoques", [])
    CACHE["precos"] = data.get("precos", [])
    CACHE["synced_at"] = data.get("synced_at")
    CACHE["ready"] = bool(CACHE["produtos"])
    return CACHE["ready"]


def _load_cache_from_seed():
    """Load cache bundled in the Docker image (cache_data.json in app directory)."""
    try:
        with open(SEED_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    CACHE["filiais"] = data.get("filiais", [])
    CACHE["produtos"] = data.get("produtos", [])
    CACHE["estoques"] = data.get("estoques", [])
    CACHE["precos"] = data.get("precos", [])
    CACHE["synced_at"] = data.get("synced_at", "bundled")
    CACHE["ready"] = bool(CACHE["produtos"])
    return CACHE["ready"]


@app.route("/api/cache/status")
def cache_status():
    if CACHE["loading"] and CACHE.get("progress"):
        progress = CACHE["progress"]
        counts = {k: progress.get(k, 0) for k in ("filiais", "produtos", "estoques", "precos")}
    else:
        counts = {"filiais": len(CACHE["filiais"]), "produtos": len(CACHE["produtos"]),
                   "estoques": len(CACHE["estoques"]), "precos": len(CACHE["precos"])}
    return jsonify({"ok": True, "ready": CACHE["ready"], "loading": CACHE["loading"],
                     "synced_at": CACHE.get("synced_at"), "counts": counts,
                     "last_error": CACHE.get("last_error")})


@app.route("/api/cache/<entity>")
def cache_get(entity):
    if entity not in CACHE:
        return jsonify({"ok": False, "error": f"Entidade '{entity}' nao existe no cache"}), 404
    return jsonify({"ok": True, "data": CACHE[entity]})


@app.route("/api/cache/reload", methods=["POST"])
def cache_reload():
    if CACHE["loading"]:
        return jsonify({"ok": False, "error": "Cache ja esta sendo carregado"}), 409
    threading.Thread(target=refresh_cache, daemon=True).start()
    return jsonify({"ok": True, "message": "Recarregando cache em background"})


DEBUG_ENTITIES = ["filiais", "produtos", "produtos_estoques", "precos", "pedidos_produtos", "clientes", "usuarios"]


@app.route("/api/debug/test-connection")
def debug_test_connection():
    results = []
    for entity in DEBUG_ENTITIES:
        _rate_limit()
        start = time.time()
        try:
            resp = requests.get(f"{API_BASE}/{entity}", headers=HEADERS,
                              params={"page": 1, "per_page": 1}, timeout=20)
            elapsed = round(time.time() - start, 2)
            body = resp.json()
            data = body.get("data", [])
            sample = data[0] if data else {}
            results.append({
                "entity": entity, "status": resp.status_code, "ok": bool(body.get("ok")),
                "total": body.get("total"), "elapsed_s": elapsed,
                "campos": sorted(sample.keys()),
            })
        except Exception as exc:
            results.append({"entity": entity, "status": None, "ok": False, "error": str(exc)})
    return jsonify({"ok": True, "results": results})


@app.route("/api/debug/pull200")
def debug_pull200():
    """Amostra do CACHE ja sincronizado (uma unica vez, compartilhado por todos os usuarios).
    Nao faz nenhuma chamada nova a API — usa exatamente os mesmos dados que /api/cache/produtos serve."""
    start = time.time()
    per_page = request.args.get("per_page", default=200, type=int)

    if not CACHE.get("ready"):
        return jsonify({"ok": False, "error": "Cache ainda nao sincronizado. Aguarde a sincronizacao inicial."}), 409

    produtos = CACHE.get("produtos", [])[:per_page]
    ids = {p["id"] for p in produtos}

    estoque_by_produto = {}
    for e in CACHE.get("estoques", []):
        if e.get("idproduto") in ids:
            estoque_by_produto.setdefault(e["idproduto"], []).append(e)

    precos_by_produto = {}
    for pr in CACHE.get("precos", []):
        if pr.get("produto") in ids:
            precos_by_produto.setdefault(pr["produto"], []).append(pr)

    enriched = [{
        **p,
        "_estoques": estoque_by_produto.get(p["id"], []),
        "_estoque_total": sum(e.get("qtd") or 0 for e in estoque_by_produto.get(p["id"], [])),
        "_precos": precos_by_produto.get(p["id"], []),
    } for p in produtos]

    return jsonify({
        "ok": True,
        "total_no_cache": len(CACHE.get("produtos", [])),
        "quantidade_puxada": len(enriched),
        "tempo_segundos": round(time.time() - start, 2),
        "sincronizado_em": CACHE.get("synced_at"),
        "cache_estoques_disponivel": len(CACHE.get("estoques", [])) > 0,
        "cache_precos_disponivel": len(CACHE.get("precos", [])) > 0,
        "produtos": enriched,
    })


def _load_audit():
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_audit(sessions):
    with open(AUDIT_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False)


@app.route("/api/audit/sessions", methods=["GET"])
def audit_sessions():
    sessions = _load_audit()
    return jsonify({"ok": True, "sessions": list(sessions.values())})


@app.route("/api/audit/session/start", methods=["POST"])
def audit_session_start():
    data = request.get_json(silent=True) or {}
    filial_id = data.get("filialId")
    filial_nome = data.get("filialNome") or ""
    user_email = (data.get("userEmail") or "").strip().lower()
    user_name = (data.get("userName") or "").strip()
    if filial_id is None or not user_email:
        return jsonify({"ok": False, "error": "filialId e userEmail sao obrigatorios."}), 400
    if user_email not in _load_users():
        return jsonify({"ok": False, "error": "Usuario nao cadastrado."}), 403
    with _audit_lock:
        sessions = _load_audit()
        hoje = date.today().isoformat()
        # Sessao compartilhada por loja+dia: bipadores da mesma loja no mesmo dia
        # caem na mesma sessao (nao criam uma nova a cada login), para verem uns
        # aos outros o que ja foi confirmado/contado.
        existing = next((s for s in sessions.values()
                          if s["filialId"] == filial_id and s["data"] == hoje), None)
        if existing:
            return jsonify({"ok": True, "session": existing}), 200
        session_id = f"audit_{filial_id}_{hoje}_{int(time.time() * 1000)}"
        session = {
            "id": session_id, "userEmail": user_email, "userName": user_name,
            "filialId": filial_id, "filialNome": filial_nome, "data": hoje,
            "inicio": datetime.now().strftime("%H:%M:%S"),
            "encontrados": {},
        }
        sessions[session_id] = session
        _save_audit(sessions)
    return jsonify({"ok": True, "session": session}), 201


@app.route("/api/audit/scan", methods=["POST"])
def audit_scan():
    data = request.get_json(silent=True) or {}
    session_id = data.get("sessionId")
    product_id = data.get("productId")
    ean = data.get("ean") or ""
    descricao = data.get("descricao") or ""
    if not session_id or product_id is None:
        return jsonify({"ok": False, "error": "sessionId e productId sao obrigatorios."}), 400
    with _audit_lock:
        sessions = _load_audit()
        session = sessions.get(session_id)
        if not session:
            return jsonify({"ok": False, "error": "Sessao de auditoria nao encontrada."}), 404
        pid = str(product_id)
        is_dup = pid in session["encontrados"]
        if not is_dup:
            session["encontrados"][pid] = {
                "ean": ean, "descricao": descricao,
                "scannedAt": datetime.now().strftime("%H:%M:%S"),
            }
            _save_audit(sessions)
    return jsonify({"ok": True, "dup": is_dup, "entry": session["encontrados"][pid]})


def _estoque_sistema(product_id, filial_id):
    total = 0
    for e in CACHE.get("estoques", []):
        if e.get("idproduto") == product_id and e.get("filial") == filial_id:
            total += e.get("qtd") or 0
    return total


@app.route("/api/audit/count", methods=["POST"])
def audit_count():
    data = request.get_json(silent=True) or {}
    session_id = data.get("sessionId")
    product_id = data.get("productId")
    ean = (data.get("ean") or "")[:64]
    descricao = (data.get("descricao") or "")[:200]
    has_delta = "delta" in data
    has_qtd = "qtd" in data

    if not session_id or product_id is None:
        return jsonify({"ok": False, "error": "sessionId e productId sao obrigatorios."}), 400
    try:
        int(product_id)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "productId deve ser um numero."}), 400
    if has_delta and has_qtd:
        return jsonify({"ok": False, "error": "Informe apenas um de delta ou qtd, nao os dois."}), 400
    if not has_delta and not has_qtd:
        return jsonify({"ok": False, "error": "Informe delta ou qtd."}), 400
    if has_delta and data.get("delta") not in (1, -1):
        return jsonify({"ok": False, "error": "delta deve ser 1 ou -1."}), 400
    if has_qtd and (not isinstance(data.get("qtd"), int) or data.get("qtd") < 0):
        return jsonify({"ok": False, "error": "qtd deve ser um inteiro >= 0."}), 400

    with _audit_lock:
        sessions = _load_audit()
        session = sessions.get(session_id)
        if not session:
            return jsonify({"ok": False, "error": "Sessao de auditoria nao encontrada."}), 404
        pid = str(product_id)
        entry = session["encontrados"].get(pid)
        if not entry:
            entry = session["encontrados"][pid] = {
                "ean": ean, "descricao": descricao,
                "scannedAt": datetime.now().strftime("%H:%M:%S"), "qtd": 0,
            }
        if has_qtd:
            entry["qtd"] = data["qtd"]
        else:
            entry["qtd"] = max(0, entry.get("qtd", 0) + data["delta"])
        _save_audit(sessions)
        qtd_sistema = _estoque_sistema(int(product_id), session["filialId"])

    return jsonify({
        "ok": True,
        "qtd": entry["qtd"],
        "qtdSistema": qtd_sistema,
        "diferenca": entry["qtd"] - qtd_sistema,
        "qtdSistemaDisponivel": bool(CACHE.get("estoques")),
    })


def _normalize_descricao(text):
    text = str(text or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    return " ".join(text.split())


def _group_signature(product_ids):
    return "-".join(str(pid) for pid in sorted(product_ids))


def _signature_to_ids(signature):
    return [int(x) for x in signature.split("-")]


def _load_dedup():
    try:
        with open(DEDUP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_dedup(data):
    with open(DEDUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _ean_valida(ean):
    """Retorna o EAN normalizado, ou string vazia se for placeholder/ausente.

    O i9logic usa o literal "SEM GTIN" no campo ean para produtos sem codigo de
    barras real. Tratar esse valor como EAN valido faria o bucketing agrupar
    todo produto sem GTIN como se fosse duplicata de qualquer outro sem GTIN.
    """
    ean = str(ean or "").strip()
    return ean if ean.upper() != "SEM GTIN" else ""


def _find_duplicate_candidates(filial_id):
    stocked_ids = {e.get("idproduto") for e in CACHE.get("estoques", []) if e.get("filial") == filial_id}
    products = [p for p in CACHE.get("produtos", []) if p.get("id") in stocked_ids]
    by_id = {p["id"]: p for p in products}
    if len(by_id) < 2:
        return []

    decided = _load_dedup().get(str(filial_id), {})

    uf = _UnionFind(by_id.keys())

    by_ean = {}
    for p in products:
        ean = _ean_valida(p.get("ean"))
        if ean:
            by_ean.setdefault(ean, []).append(p["id"])
    for ids in by_ean.values():
        if len(ids) < 2:
            continue
        base = ids[0]
        for other in ids[1:]:
            uf.union(base, other)

    components = {}
    for pid in by_id:
        root = uf.find(pid)
        components.setdefault(root, []).append(pid)

    candidates = []
    for member_ids in components.values():
        if len(member_ids) < 2:
            continue
        signature = _group_signature(member_ids)
        if signature in decided:
            continue

        signals_set = set()
        pair_effective_scores = []
        for i in range(len(member_ids)):
            for j in range(i + 1, len(member_ids)):
                a_id, b_id = member_ids[i], member_ids[j]
                a, b = by_id[a_id], by_id[b_id]
                ean_a, ean_b = _ean_valida(a.get("ean")), _ean_valida(b.get("ean"))
                score = fuzz.token_sort_ratio(
                    _normalize_descricao(a.get("descricao")),
                    _normalize_descricao(b.get("descricao")),
                )
                if score >= 90:
                    signals_set.add("descricao_muito_similar")
                if ean_a and ean_a == ean_b:
                    signals_set.add("ean_igual")
                    # EAN identico e um sinal forte, mas o catalogo real tem casos de
                    # codigo reaproveitado/gerado por engano entre produtos sem nenhuma
                    # relacao (ex: cera depilatoria e alcool com o mesmo EAN). Exigir uma
                    # semelhanca minima de descricao antes de conceder score 100 evita que
                    # esses pares virem confianca alta sem revisao individual. Piso
                    # calibrado com pares reais do catalogo: duplicatas verdadeiras
                    # ficaram >= 69, pares sem relacao nenhuma ficaram <= 35 - 50 fica no
                    # meio da folga.
                    pair_effective_scores.append(100 if score >= 50 else score)
                    continue
                pair_effective_scores.append(score)

        # Confiança do grupo = pior par (min), nunca o melhor: um unico par fracamente
        # relacionado (mesmo que so transitivamente no mesmo grupo) rebaixa o grupo inteiro.
        min_score = min(pair_effective_scores) if pair_effective_scores else 0
        if min_score >= 90:
            confidence = "alta"
        elif min_score >= 75:
            confidence = "media"
        else:
            confidence = "baixa"

        members = []
        for pid in sorted(member_ids):
            p = by_id[pid]
            members.append({
                "id": pid,
                "descricao": p.get("descricao"),
                "ean": p.get("ean"),
                "ncm": p.get("ncm"),
                "marca": p.get("marca"),
                "codproduto": p.get("codproduto"),
                "estoqueFilial": _estoque_sistema(pid, filial_id),
            })

        candidates.append({
            "signature": signature,
            "memberProductIds": sorted(member_ids),
            "confidence": confidence,
            "signals": sorted(signals_set),
            "members": members,
        })

    order = {"alta": 0, "media": 1, "baixa": 2}
    candidates.sort(key=lambda c: (order[c["confidence"]], -len(c["members"])))
    return candidates


def _load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if not _admin_password_ok(password):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha incorreta."}), 401
    _admin_login_attempts.pop(ip, None)
    return jsonify({"ok": True})


@app.route("/api/admin/bipadores", methods=["POST"])
def admin_create_bipador():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    admin_password = data.get("adminPassword") or ""
    if not _admin_password_ok(admin_password):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    filial_id = data.get("filialId")
    password = data.get("password")
    if not name or not email or filial_id is None or not isinstance(password, str) or not password:
        return jsonify({"ok": False, "error": "Nome, email, senha e loja são obrigatórios."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Senha deve ter pelo menos 6 caracteres."}), 400
    with _users_lock:
        users = _load_users()
        if email in users:
            return jsonify({"ok": False, "error": "Email já cadastrado."}), 409
        users[email] = {
            "name": name, "email": email, "filialId": filial_id,
            "password_hash": generate_password_hash(password),
        }
        _save_users(users)
        user_public = {k: v for k, v in users[email].items() if k != "password_hash"}
    return jsonify({"ok": True, "user": user_public}), 201


@app.route("/api/admin/bipadores/reset-password", methods=["POST"])
def admin_reset_bipador_password():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    admin_password = data.get("adminPassword") or ""
    if not _admin_password_ok(admin_password):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        return jsonify({"ok": False, "error": "Email e senha são obrigatórios."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Senha deve ter pelo menos 6 caracteres."}), 400
    with _users_lock:
        users = _load_users()
        if email not in users:
            return jsonify({"ok": False, "error": "Bipador não encontrado."}), 404
        users[email]["password_hash"] = generate_password_hash(password)
        _save_users(users)
    return jsonify({"ok": True})


@app.route("/api/admin/bipadores/update-filial", methods=["POST"])
def admin_update_bipador_filial():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    admin_password = data.get("adminPassword") or ""
    if not _admin_password_ok(admin_password):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    email = (data.get("email") or "").strip().lower()
    filial_id = data.get("filialId")
    if not email or filial_id is None:
        return jsonify({"ok": False, "error": "Email e filialId sao obrigatorios."}), 400

    with _users_lock:
        users = _load_users()
        if email not in users:
            return jsonify({"ok": False, "error": "Bipador nao encontrado."}), 404
        users[email]["filialId"] = filial_id
        _save_users(users)

    return jsonify({"ok": True, "filialId": filial_id})


@app.route("/api/admin/dedup/analyze", methods=["POST"])
def dedup_analyze():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    admin_password = data.get("adminPassword") or ""
    if not _admin_password_ok(admin_password):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    filial_id = data.get("filialId")
    if filial_id is None:
        return jsonify({"ok": False, "error": "filialId e obrigatorio."}), 400
    candidates = _find_duplicate_candidates(filial_id)
    return jsonify({"ok": True, "candidates": candidates})


@app.route("/api/admin/dedup/confirm", methods=["POST"])
def dedup_confirm():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    admin_password = data.get("adminPassword") or ""
    if not _admin_password_ok(admin_password):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    filial_id = data.get("filialId")
    signature = data.get("signature")
    canonical_id = data.get("canonicalProductId")
    if filial_id is None or not signature or canonical_id is None:
        return jsonify({"ok": False, "error": "filialId, signature e canonicalProductId sao obrigatorios."}), 400
    try:
        member_ids = _signature_to_ids(signature)
    except (ValueError, AttributeError, TypeError):
        return jsonify({"ok": False, "error": "signature invalida."}), 400
    if canonical_id not in member_ids:
        return jsonify({"ok": False, "error": "canonicalProductId deve ser um dos membros do grupo."}), 400

    with _dedup_lock:
        dedup = _load_dedup()
        filial_groups = dedup.setdefault(str(filial_id), {})
        filial_groups[signature] = {
            "status": "approved",
            "memberProductIds": member_ids,
            "canonicalProductId": canonical_id,
            "decidedAt": datetime.now().isoformat(),
        }
        _save_dedup(dedup)
    return jsonify({"ok": True})


@app.route("/api/admin/dedup/reject", methods=["POST"])
def dedup_reject():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    admin_password = data.get("adminPassword") or ""
    if not _admin_password_ok(admin_password):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    filial_id = data.get("filialId")
    signature = data.get("signature")
    if filial_id is None or not signature:
        return jsonify({"ok": False, "error": "filialId e signature sao obrigatorios."}), 400
    try:
        member_ids = _signature_to_ids(signature)
    except (ValueError, AttributeError, TypeError):
        return jsonify({"ok": False, "error": "signature invalida."}), 400

    with _dedup_lock:
        dedup = _load_dedup()
        filial_groups = dedup.setdefault(str(filial_id), {})
        filial_groups[signature] = {
            "status": "rejected",
            "memberProductIds": member_ids,
            "decidedAt": datetime.now().isoformat(),
        }
        _save_dedup(dedup)
    return jsonify({"ok": True})


@app.route("/api/dedup/groups")
def dedup_groups():
    filial_id = request.args.get("filialId", type=int)
    if filial_id is None:
        return jsonify({"ok": False, "error": "filialId e obrigatorio."}), 400
    dedup = _load_dedup()
    filial_groups = dedup.get(str(filial_id), {})
    produtos_by_id = {p.get("id"): p for p in CACHE.get("produtos", [])}
    approved = []
    for signature, group in filial_groups.items():
        if group.get("status") != "approved":
            continue
        members = []
        for pid in group.get("memberProductIds", []):
            p = produtos_by_id.get(pid)
            if p:
                members.append({
                    "id": pid, "descricao": p.get("descricao"), "ean": p.get("ean"),
                    "codproduto": p.get("codproduto"),
                })
        approved.append({
            "signature": signature,
            "canonicalProductId": group.get("canonicalProductId"),
            "members": members,
        })
    return jsonify({"ok": True, "groups": approved})


@app.route("/api/auth/login", methods=["POST"])
def login():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_bipador_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        return jsonify({"ok": False, "error": "Email e senha são obrigatórios."}), 400
    users = _load_users()
    user = users.get(email)
    hash_to_check = (user or {}).get("password_hash") or _DUMMY_PASSWORD_HASH
    password_ok = check_password_hash(hash_to_check, password)
    if not user or not user.get("password_hash") or not password_ok:
        _record_failed_attempt(_bipador_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Email ou senha incorretos."}), 401
    _bipador_login_attempts.pop(ip, None)
    user_public = {k: v for k, v in user.items() if k != "password_hash"}
    return jsonify({"ok": True, "user": user_public})


@app.route("/api/auth/users", methods=["GET"])
def list_users():
    users = _load_users()
    public = [{k: v for k, v in u.items() if k != "password_hash"} for u in users.values()]
    return jsonify({"ok": True, "users": public})


def _load_sales_cache():
    try:
        with open(SALES_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sales_cache(cache):
    with open(SALES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def _fetch_sales_stats(idproduto):
    stats = {"idproduto": idproduto, "total_sales": 0, "total_qty": 0, "total_value": 0,
             "last_sale_date": None, "days_without_sale": None, "first_sale_date": None,
             "sales_by_filial": {}}
    pedido_ids = []
    page = 1
    while True:
        _rate_limit()
        resp = requests.get(f"{API_BASE}/pedidos_produtos", headers=HEADERS,
                          params={"idproduto": idproduto, "page": page, "per_page": 200}, timeout=45)
        body = resp.json()
        if not body.get("ok"):
            break
        data = body.get("data", [])
        for item in data:
            qtd = item.get("qtd") or 0
            valor = item.get("valorvenda") or 0
            stats["total_qty"] += qtd
            stats["total_value"] += valor * qtd
            stats["total_sales"] += 1
            if len(pedido_ids) < 5:
                pedido_ids.append(item["idpedido"])
        total = body.get("total", 0)
        if len(stats) >= total or len(data) == 0:
            stats["total_sales"] = total
            break
        page += 1

    if pedido_ids:
        dates = []
        for pid in pedido_ids[:3]:
            _rate_limit()
            r = requests.get(f"{API_BASE}/pedidos/{pid}", headers=HEADERS, timeout=45)
            b = r.json()
            if b.get("ok"):
                d = b.get("data", {}).get("data")
                if d:
                    dates.append(d)
        if dates:
            dates.sort(reverse=True)
            stats["last_sale_date"] = dates[0]
            stats["first_sale_date"] = dates[-1]
            last_dt = datetime.strptime(dates[0], "%Y-%m-%d").date()
            stats["days_without_sale"] = (date.today() - last_dt).days

    return stats


@app.route("/api/sales/<int:idproduto>")
def product_sales(idproduto):
    cache = _load_sales_cache()
    cached = cache.get(str(idproduto))
    if cached:
        return jsonify({"ok": True, "stats": cached, "cached": True})

    stats = _fetch_sales_stats(idproduto)
    with _sales_cache_lock:
        cache = _load_sales_cache()
        cache[str(idproduto)] = stats
        _save_sales_cache(cache)

    return jsonify({"ok": True, "stats": stats, "cached": False})


@app.route("/api/sales-cache")
def sales_cache_bulk():
    return jsonify({"ok": True, "cache": _load_sales_cache()})


@app.route("/")
def index():
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    return send_file(os.path.join(template_dir, "index.html"))


if __name__ == "__main__":
    print("Servidor iniciado em http://localhost:5000")
    if _load_cache_from_disk():
        if len(CACHE.get("produtos", [])) > 0:
            print(f"Cache carregado do disco: {len(CACHE['produtos'])} produtos (sincronizado em {CACHE.get('synced_at')}).")
        else:
            print("Cache em disco vazio, iniciando sincronizacao com API...")
            threading.Thread(target=refresh_cache, daemon=True).start()
    elif _load_cache_from_seed():
        print(f"Cache carregado do seed: {len(CACHE['produtos'])} produtos. Salvando no volume...")
        _save_cache_to_disk()
    else:
        threading.Thread(target=refresh_cache, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
