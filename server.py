import collections
import hmac
import json
import os
import time
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
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

# Limite documentado da API: 30 requisicoes/minuto por credencial. O limitador
# antigo (sleep fixo de 3s entre chamadas, sem lock) so dava ~20/min e nao era
# thread-safe sob concorrencia - varias threads lendo/escrevendo a mesma
# variavel global sem protecao. Este e um limitador de janela deslizante
# (sliding window) com lock, compartilhado por todas as threads: cada uma
# registra seu timestamp e, se a janela dos ultimos 60s ja tiver
# _RATE_LIMIT_MAX chamadas, dorme (fora do lock, pra nao bloquear as outras)
# so o tempo necessario. Isso permite varios workers em paralelo sem
# ultrapassar o teto real da API - mais rapido que o antigo, mas nunca acima
# do que a i9logic realmente permite.
_RATE_LIMIT_MAX = 28
_RATE_LIMIT_WINDOW = 60.0
_rate_limit_lock = threading.Lock()
_rate_limit_timestamps = collections.deque()


def _rate_limit():
    while True:
        with _rate_limit_lock:
            now = time.time()
            while _rate_limit_timestamps and now - _rate_limit_timestamps[0] > _RATE_LIMIT_WINDOW:
                _rate_limit_timestamps.popleft()
            if len(_rate_limit_timestamps) < _RATE_LIMIT_MAX:
                _rate_limit_timestamps.append(now)
                return
            wait = _RATE_LIMIT_WINDOW - (now - _rate_limit_timestamps[0]) + 0.05
        time.sleep(max(wait, 0.05))


def _get_com_retry(path, params):
    """GET com o mesmo retry/backoff exponencial de _paginated_fetch (10
    tentativas, espera crescente ate 120s) para rotinas longas que rodam sem
    supervisao - sem isso, um unico RATE_LIMIT_EXCEEDED temporario derruba a
    rotina inteira em vez de so esperar e continuar."""
    for attempt in range(10):
        _rate_limit()
        resp = requests.get(f"{API_BASE}/{path}", headers=HEADERS, params=params, timeout=45)
        body = resp.json()
        if resp.status_code == 429 or (not body.get("ok") and "RATE_LIMIT" in str(body.get("error", ""))):
            wait = min(30 * (2 ** attempt), 120)
            print(f"[retry] rate-limit em {path}, tentativa {attempt+1}/10, aguardando {wait}s...")
            time.sleep(wait)
            continue
        return body
    return body


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


FASE2_FILE = os.path.join(DATA_DIR, "fase2_liberada.json")


@app.route("/api/audit/fase2", methods=["GET"])
def audit_fase2_status():
    return jsonify({"ok": True, "liberadas": _load_json_file(FASE2_FILE, {})})


@app.route("/api/audit/fase2/liberar", methods=["POST"])
def audit_fase2_liberar():
    data = request.get_json(silent=True) or {}
    filial_id = data.get("filialId")
    if filial_id is None:
        return jsonify({"ok": False, "error": "filialId e obrigatorio."}), 400
    with _audit_lock:
        liberadas = _load_json_file(FASE2_FILE, {})
        liberadas[str(filial_id)] = True
        _save_json_file(FASE2_FILE, liberadas)
    return jsonify({"ok": True, "liberadas": liberadas})


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


def _com_dias_sem_vender(stats):
    """days_without_sale persistido fica velho com o tempo (calculado no dia
    em que a linha foi escrita) - recalcula sempre a partir de last_sale_date
    e da data de hoje, sem tocar no arquivo em disco."""
    last_sale_date = stats.get("last_sale_date")
    if not last_sale_date:
        return stats
    try:
        last_dt = datetime.strptime(last_sale_date, "%Y-%m-%d").date()
    except ValueError:
        return stats
    return {**stats, "days_without_sale": (date.today() - last_dt).days}


@app.route("/api/sales/<int:idproduto>")
def product_sales(idproduto):
    cache = _load_sales_cache()
    cached = cache.get(str(idproduto))
    if cached:
        return jsonify({"ok": True, "stats": _com_dias_sem_vender(cached), "cached": True})

    stats = _fetch_sales_stats(idproduto)
    with _sales_cache_lock:
        cache = _load_sales_cache()
        cache[str(idproduto)] = stats
        _save_sales_cache(cache)

    return jsonify({"ok": True, "stats": _com_dias_sem_vender(stats), "cached": False})


def _fetch_pedidos_produtos_pagina(idproduto, page):
    return _get_com_retry("pedidos_produtos", {"idproduto": idproduto, "page": page, "per_page": 200})


def _fetch_pedido_data(idpedido):
    return _get_com_retry(f"pedidos/{idpedido}", {})


@app.route("/api/sales-raw/<int:idproduto>")
def sales_raw(idproduto):
    """Proxy de uma pagina de pedidos_produtos para uso externo (debug/scripts
    pontuais) que precise iterar TODOS os itens historicos, nao so o resumo
    agregado de /api/sales. Usa o mesmo _rate_limit() do processo, entao nunca
    dispara chamada paralela descoordenada com outras rotinas em andamento."""
    page = request.args.get("page", 1, type=int)
    body = _fetch_pedidos_produtos_pagina(idproduto, page)
    if not body.get("ok"):
        return jsonify({"ok": False, "error": body.get("error") or "falha na API i9logic"}), 502
    return jsonify({"ok": True, "data": body.get("data", []), "total": body.get("total", 0)})


@app.route("/api/pedido-data/<int:idpedido>")
def pedido_data(idpedido):
    """Proxy pontual da data de um pedido, mesmo uso do endpoint acima."""
    body = _fetch_pedido_data(idpedido)
    if not body.get("ok"):
        return jsonify({"ok": False, "error": body.get("error") or "falha na API i9logic"}), 502
    return jsonify({"ok": True, "data": body.get("data", {}).get("data")})


VENDAS_2026_FILE = os.path.join(DATA_DIR, "vendas_2026_por_produto.json")
PEDIDO_DATAS_FILE = os.path.join(DATA_DIR, "pedido_datas_cache.json")
_vendas_2026_lock = threading.Lock()
VENDAS_2026_STATE = {"running": False, "processed": 0, "total": 0, "started_at": None,
                      "finished_at": None, "last_error": None}


def _load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _run_vendas_2026(filial_id):
    """Calcula, para cada produto ja bipado na sessao mais recente da loja
    informada, quanto foi vendido em 2026 (qtd/valor/numero de vendas). Nao ha
    filtro de data no endpoint de pedidos da API i9logic, entao a unica forma
    confiavel e abrir cada pedido individual e olhar a data. Roda numa thread
    daemon separada (chamada via /api/admin/sales-2026/start) porque para um
    catalogo bipado de centenas de produtos isso pode levar horas - persiste
    incrementalmente em disco para nunca perder progresso caso o processo
    reinicie no meio (redeploy, restart)."""
    sessions = _load_audit()
    session = next((s for s in sessions.values() if s["filialId"] == filial_id), None)
    ids = list(session["encontrados"].keys()) if session else []

    pedido_datas = _load_json_file(PEDIDO_DATAS_FILE, {})
    resultado = _load_json_file(VENDAS_2026_FILE, {})

    VENDAS_2026_STATE["running"] = True
    VENDAS_2026_STATE["total"] = len(ids)
    VENDAS_2026_STATE["processed"] = sum(1 for pid in ids if resultado.get(pid, {}).get("processado"))
    VENDAS_2026_STATE["started_at"] = datetime.now().isoformat()
    VENDAS_2026_STATE["finished_at"] = None
    VENDAS_2026_STATE["last_error"] = None

    try:
        for pid in ids:
            if resultado.get(pid, {}).get("processado"):
                continue

            itens = []
            page = 1
            while True:
                body = _fetch_pedidos_produtos_pagina(int(pid), page)
                if not body.get("ok"):
                    break
                data = body.get("data", [])
                itens.extend(data)
                total = body.get("total", 0)
                if len(itens) >= total or not data:
                    break
                page += 1

            qtd_2026 = 0
            valor_2026 = 0.0
            vendas_2026 = 0
            novos_pedidos = 0
            for item in itens:
                idpedido = str(item["idpedido"])
                if idpedido not in pedido_datas:
                    b = _fetch_pedido_data(int(idpedido))
                    pedido_datas[idpedido] = b.get("data", {}).get("data") if b.get("ok") else None
                    novos_pedidos += 1
                    if novos_pedidos % 25 == 0:
                        _save_json_file(PEDIDO_DATAS_FILE, pedido_datas)
                d = pedido_datas.get(idpedido)
                if d and d.startswith("2026"):
                    qtd = item.get("qtd") or 0
                    valor = item.get("valorvenda") or 0
                    qtd_2026 += qtd
                    valor_2026 += valor * qtd
                    vendas_2026 += 1

            resultado[pid] = {
                "qtd_2026": qtd_2026,
                "valor_2026": round(valor_2026, 2),
                "vendas_2026": vendas_2026,
                "total_itens_historico": len(itens),
                "processado": True,
            }
            _save_json_file(VENDAS_2026_FILE, resultado)
            _save_json_file(PEDIDO_DATAS_FILE, pedido_datas)
            VENDAS_2026_STATE["processed"] += 1
    except Exception as exc:
        VENDAS_2026_STATE["last_error"] = str(exc)
    finally:
        VENDAS_2026_STATE["running"] = False
        VENDAS_2026_STATE["finished_at"] = datetime.now().isoformat()


@app.route("/api/admin/sales-2026/start", methods=["POST"])
def admin_sales_2026_start():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429
    data = request.get_json(silent=True) or {}
    if not _admin_password_ok(data.get("adminPassword")):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    filial_id = data.get("filialId")
    if filial_id is None:
        return jsonify({"ok": False, "error": "filialId e obrigatorio."}), 400

    with _vendas_2026_lock:
        if VENDAS_2026_STATE["running"]:
            return jsonify({"ok": False, "error": "Calculo ja esta rodando."}), 409
        threading.Thread(target=_run_vendas_2026, args=(filial_id,), daemon=True).start()
    return jsonify({"ok": True, "message": "Calculo de vendas 2026 iniciado em background."})


@app.route("/api/sales-2026/status")
def sales_2026_status():
    resultado = _load_json_file(VENDAS_2026_FILE, {})
    return jsonify({"ok": True, "state": VENDAS_2026_STATE, "resultado": resultado})


PEDIDOS_2026_IDS_FILE = os.path.join(DATA_DIR, "pedidos_2026_ids.json")
VENDAS_2026_CATALOGO_FILE = os.path.join(DATA_DIR, "vendas_2026_catalogo.json")
PEDIDOS_2026_PROCESSADOS_FILE = os.path.join(DATA_DIR, "pedidos_2026_processados.json")
_vendas_2026_full_lock = threading.Lock()
VENDAS_2026_FULL_STATE = {"running": False, "phase": None, "processed": 0, "total": 0,
                           "started_at": None, "finished_at": None, "last_error": None}


def _fetch_pedidos_por_data_pagina(page):
    return _get_com_retry("pedidos", {"data_de": "2026-01-01", "data_ate": "2026-12-31",
                                       "page": page, "per_page": 200})


def _fetch_pedidos_produtos_by_idpedido(idpedido, page):
    return _get_com_retry("pedidos_produtos", {"idpedido": idpedido, "page": page, "per_page": 200})


def _run_vendas_2026_full():
    """Mesmo objetivo de _run_vendas_2026, mas para o catalogo inteiro em vez
    de so os produtos ja bipados numa loja. Abordagem bem mais eficiente,
    descoberta na documentacao publica da API (api.i9logic.net): o endpoint
    'pedidos' aceita filtro por data (data_de/data_ate), diferente de
    'pedidos_produtos' que nao tem campo de data. Entao em vez de abrir cada
    pedido do HISTORICO INTEIRO de cada produto (abordagem de _run_vendas_2026,
    inviavel em escala de catalogo - lariam semanas), aqui:
    1. Lista so os pedidos de 2026 (rapido, ~185 mil pedidos / 200 por pagina).
    2. Para cada um desses pedidos (nao mais por produto), busca os itens e
       soma no produto correspondente.
    Persiste a lista de IDs de pedidos de 2026 uma vez (fase 1) e depois vai
    marcando cada pedido como processado (fase 2), para retomar exatamente de
    onde parou se o processo reiniciar no meio dos ~6 dias estimados."""
    VENDAS_2026_FULL_STATE["running"] = True
    VENDAS_2026_FULL_STATE["started_at"] = datetime.now().isoformat()
    VENDAS_2026_FULL_STATE["finished_at"] = None
    VENDAS_2026_FULL_STATE["last_error"] = None

    try:
        ids = _load_json_file(PEDIDOS_2026_IDS_FILE, None)
        if ids is None:
            VENDAS_2026_FULL_STATE["phase"] = "listando_pedidos"
            ids = []
            page = 1
            while True:
                body = _fetch_pedidos_por_data_pagina(page)
                if not body.get("ok"):
                    raise RuntimeError(f"falha ao listar pedidos pagina {page}: {body.get('error')}")
                data = body.get("data", [])
                ids.extend(item["id"] for item in data)
                total = body.get("total", 0)
                VENDAS_2026_FULL_STATE["processed"] = len(ids)
                VENDAS_2026_FULL_STATE["total"] = total
                if len(ids) >= total or not data:
                    break
                page += 1
            _save_json_file(PEDIDOS_2026_IDS_FILE, ids)

        VENDAS_2026_FULL_STATE["phase"] = "detalhando_pedidos"
        resultado = _load_json_file(VENDAS_2026_CATALOGO_FILE, {})
        processados = set(_load_json_file(PEDIDOS_2026_PROCESSADOS_FILE, []))
        VENDAS_2026_FULL_STATE["total"] = len(ids)
        VENDAS_2026_FULL_STATE["processed"] = len(processados)

        for idpedido in ids:
            if idpedido in processados:
                continue

            itens = []
            page = 1
            while True:
                body = _fetch_pedidos_produtos_by_idpedido(idpedido, page)
                if not body.get("ok"):
                    break
                data = body.get("data", [])
                itens.extend(data)
                total = body.get("total", 0)
                if len(itens) >= total or not data:
                    break
                page += 1

            for item in itens:
                pid = str(item["idproduto"])
                entry = resultado.setdefault(pid, {"qtd_2026": 0, "valor_2026": 0.0, "vendas_2026": 0})
                qtd = item.get("qtd") or 0
                valor = item.get("valorvenda") or 0
                entry["qtd_2026"] += qtd
                entry["valor_2026"] += valor * qtd
                entry["vendas_2026"] += 1

            processados.add(idpedido)
            VENDAS_2026_FULL_STATE["processed"] = len(processados)
            if len(processados) % 50 == 0:
                _save_json_file(VENDAS_2026_CATALOGO_FILE, resultado)
                _save_json_file(PEDIDOS_2026_PROCESSADOS_FILE, list(processados))

        _save_json_file(VENDAS_2026_CATALOGO_FILE, resultado)
        _save_json_file(PEDIDOS_2026_PROCESSADOS_FILE, list(processados))
    except Exception as exc:
        VENDAS_2026_FULL_STATE["last_error"] = str(exc)
    finally:
        VENDAS_2026_FULL_STATE["running"] = False
        VENDAS_2026_FULL_STATE["finished_at"] = datetime.now().isoformat()


@app.route("/api/admin/sales-2026-full/start", methods=["POST"])
def admin_sales_2026_full_start():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429
    data = request.get_json(silent=True) or {}
    if not _admin_password_ok(data.get("adminPassword")):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    with _vendas_2026_full_lock:
        if VENDAS_2026_FULL_STATE["running"]:
            return jsonify({"ok": False, "error": "Calculo ja esta rodando."}), 409
        threading.Thread(target=_run_vendas_2026_full, daemon=True).start()
    return jsonify({"ok": True, "message": "Calculo de vendas 2026 do catalogo inteiro iniciado em background."})


@app.route("/api/sales-2026-full/status")
def sales_2026_full_status():
    total_produtos = len(_load_json_file(VENDAS_2026_CATALOGO_FILE, {}))
    return jsonify({"ok": True, "state": VENDAS_2026_FULL_STATE, "produtos_com_venda_2026": total_produtos})


VENDAS_FULL_META_FILE = os.path.join(DATA_DIR, "vendas_full_pedidos_meta.json")
VENDAS_FULL_PROCESSADOS_FILE = os.path.join(DATA_DIR, "vendas_full_processados.json")
_vendas_full_lock = threading.Lock()
VENDAS_FULL_STATE = {"running": False, "phase": None, "processed": 0, "total": 0,
                     "data_inicio": None, "data_fim": None,
                     "started_at": None, "finished_at": None, "last_error": None}


def _fetch_pedidos_por_periodo_pagina(data_de, data_ate, page):
    return _get_com_retry("pedidos", {"data_de": data_de, "data_ate": data_ate,
                                       "page": page, "per_page": 200})


def _detalhar_pedido(idpedido):
    """Busca todos os itens (pedidos_produtos) de um pedido. Chamada em
    paralelo por varios workers em _run_vendas_full - so faz I/O de rede
    (protegido pelo _rate_limit compartilhado), nao toca em nenhum estado
    mutavel compartilhado, entao e segura pra correr concorrentemente."""
    itens = []
    page = 1
    while True:
        body = _fetch_pedidos_produtos_by_idpedido(int(idpedido), page)
        if not body.get("ok"):
            break
        data = body.get("data", [])
        itens.extend(data)
        total = body.get("total", 0)
        if len(itens) >= total or not data:
            break
        page += 1
    return idpedido, itens


def _run_vendas_full(data_de, data_ate):
    """Backfill unico de vendas por produto, escrevendo direto em
    SALES_CACHE_FILE - o mesmo arquivo que /api/sales-cache serve e que a aba
    Relatorios de fato le (o antigo _run_vendas_2026_full grava num arquivo
    separado, vendas_2026_catalogo.json, que nenhuma tela consome).

    Mesma tecnica eficiente: lista pedidos por data (pedidos aceita data como
    filtro minimo) capturando id+data+filial na mesma passada, depois abre
    pedidos_produtos de cada pedido. Diferente da rotina antiga, a data do
    pedido ja vem da fase de listagem - dispensa uma chamada extra por pedido
    so pra descobrir a data (o que _run_vendas_2026 antigo fazia).

    A listagem roda em TODA chamada (nao so na primeira) e so ACRESCENTA
    pedidos novos ao meta existente - por isso a mesma funcao serve tanto
    pro backfill historico (janela grande, uma vez) quanto pro job diario
    (janela pequena, todo dia, sempre re-listando pra pegar pedidos novos).

    Reset do cache so acontece na primeira execucao de todas (quando o
    meta.json ainda nao existia em disco), pra nao contar em dobro os
    produtos que ja tinham entrada em SALES_CACHE_FILE vinda do metodo antigo
    (_fetch_sales_stats, historico completo sem recorte de data). Em toda
    chamada seguinte (meta.json ja existe, mesmo que noutra janela de data),
    o cache e carregado normalmente e so recebe incrementos dos pedidos ainda
    nao processados - retomavel entre redeploys como as demais rotinas."""
    VENDAS_FULL_STATE["running"] = True
    VENDAS_FULL_STATE["data_inicio"] = data_de
    VENDAS_FULL_STATE["data_fim"] = data_ate
    VENDAS_FULL_STATE["started_at"] = datetime.now().isoformat()
    VENDAS_FULL_STATE["finished_at"] = None
    VENDAS_FULL_STATE["last_error"] = None

    try:
        meta = _load_json_file(VENDAS_FULL_META_FILE, None)
        primeira_execucao = meta is None
        if meta is None:
            meta = {}

        VENDAS_FULL_STATE["phase"] = "listando_pedidos"
        page = 1
        encontrados_nesta_janela = 0
        while True:
            body = _fetch_pedidos_por_periodo_pagina(data_de, data_ate, page)
            if not body.get("ok"):
                raise RuntimeError(f"falha ao listar pedidos pagina {page}: {body.get('error')}")
            data = body.get("data", [])
            for item in data:
                meta[str(item["id"])] = {"data": item.get("data"), "filial": item.get("filial_venda")}
            encontrados_nesta_janela += len(data)
            total = body.get("total", 0)
            VENDAS_FULL_STATE["processed"] = encontrados_nesta_janela
            VENDAS_FULL_STATE["total"] = total
            if encontrados_nesta_janela >= total or not data:
                break
            page += 1
        _save_json_file(VENDAS_FULL_META_FILE, meta)

        VENDAS_FULL_STATE["phase"] = "detalhando_pedidos"
        processados = set(_load_json_file(VENDAS_FULL_PROCESSADOS_FILE, []))
        cache = {} if primeira_execucao else _load_sales_cache()
        ids = list(meta.keys())
        pendentes = [i for i in ids if i not in processados]
        VENDAS_FULL_STATE["total"] = len(ids)
        VENDAS_FULL_STATE["processed"] = len(processados)

        # O teto real e o rate limit da API (30/min, ver _rate_limit), nao o
        # numero de workers - varias threads em paralelo aqui so garantem que
        # a espera de rede de uma chamada nao atrase o cronometro da proxima,
        # o suficiente pra de fato saturar os 28/min permitidos em vez dos
        # ~20/min do limitador antigo (sleep fixo single-thread).
        DETALHE_WORKERS = 6

        with ThreadPoolExecutor(max_workers=DETALHE_WORKERS) as pool:
            for idpedido, itens in pool.map(_detalhar_pedido, pendentes):
                pedido_data = meta[idpedido].get("data")
                pedido_filial = meta[idpedido].get("filial")

                for item in itens:
                    pid = str(item["idproduto"])
                    entry = cache.setdefault(pid, {
                        "idproduto": int(pid), "total_sales": 0, "total_qty": 0, "total_value": 0.0,
                        "last_sale_date": None, "first_sale_date": None, "days_without_sale": None,
                        "sales_by_filial": {},
                    })
                    qtd = item.get("qtd") or 0
                    valor = item.get("valorvenda") or 0
                    entry["total_qty"] += qtd
                    entry["total_value"] += valor * qtd
                    entry["total_sales"] += 1
                    if pedido_data:
                        if not entry["last_sale_date"] or pedido_data > entry["last_sale_date"]:
                            entry["last_sale_date"] = pedido_data
                        if not entry["first_sale_date"] or pedido_data < entry["first_sale_date"]:
                            entry["first_sale_date"] = pedido_data
                    if pedido_filial is not None:
                        fkey = str(pedido_filial)
                        entry["sales_by_filial"][fkey] = entry["sales_by_filial"].get(fkey, 0) + qtd

                processados.add(idpedido)
                VENDAS_FULL_STATE["processed"] = len(processados)
                if len(processados) % 50 == 0:
                    _save_sales_cache(cache)
                    _save_json_file(VENDAS_FULL_PROCESSADOS_FILE, list(processados))

        _save_sales_cache(cache)
        _save_json_file(VENDAS_FULL_PROCESSADOS_FILE, list(processados))
    except Exception as exc:
        VENDAS_FULL_STATE["last_error"] = str(exc)
    finally:
        VENDAS_FULL_STATE["running"] = False
        VENDAS_FULL_STATE["finished_at"] = datetime.now().isoformat()


@app.route("/api/admin/sales-full/start", methods=["POST"])
def admin_sales_full_start():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429
    data = request.get_json(silent=True) or {}
    if not _admin_password_ok(data.get("adminPassword")):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    data_de = data.get("dataDe") or "2025-01-01"
    data_ate = data.get("dataAte") or date.today().isoformat()

    with _vendas_full_lock:
        if VENDAS_FULL_STATE["running"]:
            return jsonify({"ok": False, "error": "Calculo ja esta rodando."}), 409
        threading.Thread(target=_run_vendas_full, args=(data_de, data_ate), daemon=True).start()
    return jsonify({"ok": True, "message": f"Backfill de vendas ({data_de} a {data_ate}) iniciado em background."})


@app.route("/api/sales-full/status")
def sales_full_status():
    total_produtos = len(_load_sales_cache())
    return jsonify({"ok": True, "state": VENDAS_FULL_STATE, "produtos_com_venda": total_produtos})


DAILY_SYNC_STATE_FILE = os.path.join(DATA_DIR, "daily_sync_state.json")
DAILY_SYNC_TZ = ZoneInfo("America/Sao_Paulo")
DAILY_SYNC_HOUR = 23
DAILY_SYNC_STATE = {"last_run_at": None, "last_covered_date": None, "last_error": None}


def _run_daily_sync():
    """Job diario (23:00 America/Sao_Paulo, ver _daily_scheduler_loop): puxa
    produtos novos cadastrados (refresh_cache - resync completo do catalogo,
    ja existente e usado manualmente via /api/cache/reload) e faz o backfill
    incremental de vendas ate hoje, retomando de onde o ultimo dia coberto
    parou (se o servidor ficou fora por alguns dias, cobre tudo de uma vez).

    So avanca 'last_covered_date' se _run_vendas_full terminou sem erro -
    numa falha (API fora, rate limit persistente), a mesma janela e
    reprocessada no proximo disparo em vez de ser silenciosamente perdida."""
    print("[daily-sync] iniciando sincronizacao diaria (produtos + vendas)...")
    DAILY_SYNC_STATE["last_run_at"] = datetime.now().isoformat()
    DAILY_SYNC_STATE["last_error"] = None

    if not CACHE["loading"]:
        threading.Thread(target=refresh_cache, daemon=True).start()
    else:
        print("[daily-sync] refresh de catalogo ja em andamento, nao disparei outro.")

    with _vendas_full_lock:
        if VENDAS_FULL_STATE["running"]:
            print("[daily-sync] backfill de vendas ja rodando (outra chamada) - pulando, sera coberto no proximo disparo.")
            return
        hoje = date.today()
        estado = _load_json_file(DAILY_SYNC_STATE_FILE, {})
        ultima_coberta = estado.get("ultima_data_coberta")
        if ultima_coberta:
            data_de = (date.fromisoformat(ultima_coberta) + timedelta(days=1)).isoformat()
        else:
            data_de = (hoje - timedelta(days=1)).isoformat()
        data_ate = hoje.isoformat()
        if data_de > data_ate:
            data_de = data_ate

        def _worker():
            _run_vendas_full(data_de, data_ate)
            if VENDAS_FULL_STATE.get("last_error"):
                DAILY_SYNC_STATE["last_error"] = VENDAS_FULL_STATE["last_error"]
                print(f"[daily-sync] backfill de vendas falhou: {VENDAS_FULL_STATE['last_error']} - tenta de novo no proximo disparo.")
            else:
                _save_json_file(DAILY_SYNC_STATE_FILE, {"ultima_data_coberta": data_ate})
                DAILY_SYNC_STATE["last_covered_date"] = data_ate
                print(f"[daily-sync] vendas atualizadas ate {data_ate}.")

        threading.Thread(target=_worker, daemon=True).start()


def _daily_scheduler_loop():
    """Dorme at o proximo 23:00 (America/Sao_Paulo) e dispara _run_daily_sync,
    em loop indefinido. Usa zoneinfo em vez do horario local do container
    (que normalmente roda em UTC no Docker) pra nao depender de TZ configurada
    na infra."""
    while True:
        now = datetime.now(DAILY_SYNC_TZ)
        target = now.replace(hour=DAILY_SYNC_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            _run_daily_sync()
        except Exception as exc:
            DAILY_SYNC_STATE["last_error"] = str(exc)
            print(f"[daily-sync] erro ao disparar job diario: {exc}")


@app.route("/api/daily-sync/status")
def daily_sync_status():
    now = datetime.now(DAILY_SYNC_TZ)
    next_run = now.replace(hour=DAILY_SYNC_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    estado = _load_json_file(DAILY_SYNC_STATE_FILE, {})
    return jsonify({"ok": True, "state": DAILY_SYNC_STATE,
                    "ultima_data_coberta": estado.get("ultima_data_coberta"),
                    "proximo_disparo": next_run.isoformat()})


LAST_SALE_FILE = os.path.join(DATA_DIR, "last_sale_por_produto.json")
LAST_SALE_PAGE_FILE = os.path.join(DATA_DIR, "last_sale_progress.json")
_last_sale_lock = threading.Lock()
LAST_SALE_STATE = {"running": False, "pedidos_processados": 0, "produtos_cobertos": 0,
                    "produtos_catalogo": 0, "started_at": None, "finished_at": None, "last_error": None}


def _run_last_sale_scan():
    """Numero de vendas + data da ultima venda de CADA produto do catalogo,
    de uma vez so (nao filtrado por loja - a API i9logic nao expoe filial na
    entidade pedidos_produtos). Estrategia: varre os pedidos do mais recente
    para o mais antigo (ordenacao padrao da API e id DESC, que acompanha o
    tempo); para cada pedido, olha os produtos vendidos nele. A PRIMEIRA vez
    que um produto aparece (indo do mais recente pro mais antigo) e a data da
    sua ultima venda - continua contando toda vez que reaparece. Para assim
    que cobrir quase todo o catalogo, em vez de ter que abrir o historico
    completo de cada produto individualmente (abordagem antiga, inviavel em
    escala - dias a mais de 1 mes). Produtos raros que nunca aparecerem ficam
    de fora do resultado (sem vendas no periodo varrido)."""
    resultado = _load_json_file(LAST_SALE_FILE, {})
    progress = _load_json_file(LAST_SALE_PAGE_FILE, {"next_page": 1})
    page = progress.get("next_page", 1)

    catalogo_ids = {str(p["id"]) for p in CACHE.get("produtos", [])}
    total_catalogo = len(catalogo_ids) or 1

    LAST_SALE_STATE["running"] = True
    LAST_SALE_STATE["produtos_catalogo"] = len(catalogo_ids)
    LAST_SALE_STATE["produtos_cobertos"] = len(resultado)
    LAST_SALE_STATE["pedidos_processados"] = (page - 1) * 200
    LAST_SALE_STATE["started_at"] = datetime.now().isoformat()
    LAST_SALE_STATE["finished_at"] = None
    LAST_SALE_STATE["last_error"] = None

    try:
        while len(resultado) < total_catalogo * 0.98:
            _rate_limit()
            # 'pedidos' nao aceita listagem sem filtro (REQUIRED_FILTER_MISSING) -
            # 'data' e um dos filtros minimos aceitos, entao usamos uma janela
            # bem ampla so para satisfazer a exigencia, sem de fato restringir.
            resp = requests.get(f"{API_BASE}/pedidos", headers=HEADERS,
                                params={"data_de": "2015-01-01", "data_ate": "2030-12-31",
                                        "page": page, "per_page": 200}, timeout=45)
            body = resp.json()
            if not body.get("ok"):
                raise RuntimeError(f"falha ao listar pedidos pagina {page}: {body.get('error')}")
            pedidos = body.get("data", [])
            if not pedidos:
                break

            for pedido in pedidos:
                itens_body = _fetch_pedidos_produtos_by_idpedido(pedido["id"], 1)
                if not itens_body.get("ok"):
                    continue
                for item in itens_body.get("data", []):
                    pid = str(item["idproduto"])
                    entry = resultado.setdefault(pid, {"last_sale_date": None, "vendas_count": 0})
                    entry["vendas_count"] += 1
                    if entry["last_sale_date"] is None:
                        entry["last_sale_date"] = pedido.get("data")
                LAST_SALE_STATE["pedidos_processados"] += 1

            LAST_SALE_STATE["produtos_cobertos"] = len(resultado)
            page += 1
            _save_json_file(LAST_SALE_FILE, resultado)
            _save_json_file(LAST_SALE_PAGE_FILE, {"next_page": page})
    except Exception as exc:
        LAST_SALE_STATE["last_error"] = str(exc)
    finally:
        LAST_SALE_STATE["running"] = False
        LAST_SALE_STATE["finished_at"] = datetime.now().isoformat()


@app.route("/api/admin/last-sale-scan/start", methods=["POST"])
def admin_last_sale_scan_start():
    ip = request.remote_addr or "unknown"
    limited, count, window_start = _rate_limited(_admin_login_attempts, ip)
    if limited:
        return jsonify({"ok": False, "error": "Muitas tentativas. Tente novamente em alguns minutos."}), 429
    data = request.get_json(silent=True) or {}
    if not _admin_password_ok(data.get("adminPassword")):
        _record_failed_attempt(_admin_login_attempts, ip, count, window_start)
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    _admin_login_attempts.pop(ip, None)

    with _last_sale_lock:
        if LAST_SALE_STATE["running"]:
            return jsonify({"ok": False, "error": "Calculo ja esta rodando."}), 409
        threading.Thread(target=_run_last_sale_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Calculo de vendas/ultima venda iniciado em background."})


@app.route("/api/last-sale-scan/status")
def last_sale_scan_status():
    return jsonify({"ok": True, "state": LAST_SALE_STATE})


@app.route("/api/sales-cache")
def sales_cache_bulk():
    cache = _load_sales_cache()
    return jsonify({"ok": True, "cache": {pid: _com_dias_sem_vender(s) for pid, s in cache.items()}})


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
    threading.Thread(target=_daily_scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
