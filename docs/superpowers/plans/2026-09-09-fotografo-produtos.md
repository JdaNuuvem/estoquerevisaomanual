# Fotógrafo de Produtos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar um papel de usuário "fotógrafo" com uma tela dedicada e gamificada (barra de progresso) que mostra, um de cada vez, os produtos já bipados numa loja, permite capturar a foto direto da câmera do navegador (preview ao vivo, sem abrir o app nativo) e envia — avançando sozinho pro próximo produto. Inclui um leitor de código de barras avulso pra pular pra um produto específico fora de ordem, uma aba de revisão pra substituir fotos já enviadas, e uma visão no admin com progresso por loja, galeria e download em `.zip`.

**Architecture:** Backend Flask existente (`server.py`) ganha 5 endpoints novos e um arquivo de estado (`fotos_por_filial.json`, mesmo padrão JSON-em-disco de tudo mais no projeto) mais uma pasta de arquivos (`DATA_DIR/fotos/<filialId>/<produtoId>.jpg`) no mesmo volume Docker persistente. Frontend é a mesma SPA vanilla JS (`templates/index.html`) — novo papel `role` em `users.json` desvia o pós-login pra uma view nova (`fotografo-view`), com um modal de câmera dedicado (preview `<video>` + captura via `<canvas>`, independente do modal de scanner de barcode já existente).

**Tech Stack:** Flask 3.1, pytest (testes já existentes no projeto usam `server.app.test_client()` com fixtures que isolam arquivos via `tmp_path`/`monkeypatch`), vanilla JS + `getUserMedia`/`<canvas>` no navegador, `zipfile`/`io` da stdlib pro download em lote (sem dependência nova).

**Spec:** `docs/superpowers/specs/2026-09-09-fotografo-produtos-design.md`

## Global Constraints

- Toda rota de escrita que hoje exige `adminPassword` (endpoints `/api/admin/...`) continua exigindo — nenhum endpoint novo de admin fica sem essa checagem.
- `DATA_DIR` é o único lugar onde arquivos persistem entre redeploys (volume Docker) — a pasta de fotos vive dentro dele (`DATA_DIR/fotos/`), nunca em caminho relativo ao código-fonte.
- `filialId` e `produtoId` em qualquer endpoint novo são sempre convertidos com `int(...)` antes de virar parte de um caminho de arquivo (mesmo padrão de validação já usado em `product_id` nos endpoints de auditoria) — nunca usar o valor cru do request num path.
- Todo teste novo segue o padrão de `tests/test_admin_bipador_flow.py`: fixture `client` monkeypatchando os `_FILE`/`_DIR` do módulo `server` pra `tmp_path`, nunca tocando nos arquivos reais do projeto.
- Frontend não tem suite de testes automatizada neste projeto (confirmado — só `tests/*.py` existe) — cada tarefa de frontend termina com uma verificação manual explícita (servidor local + Playwright ou navegador), não pytest.

---

## Task 1: Papel de usuário (`role`) no cadastro

**Files:**
- Modify: `server.py` (`admin_create_bipador`, por volta da linha 739)
- Test: `tests/test_fotografo.py` (novo arquivo)

**Interfaces:**
- Produces: `admin_create_bipador` passa a aceitar `role` opcional no JSON do POST (`"bipador"` | `"fotografo"`, default `"bipador"` se ausente/vazio/inválido); o objeto salvo em `users.json` ganha a chave `"role"`; `user_public` retornado já inclui `role` (nenhuma mudança de filtro necessária, já retorna todas as chaves menos `password_hash`).

- [ ] **Step 1: Escrever o teste que falha**

```python
"""Testes do papel fotografo: cadastro com role, fila, upload de foto,
servir/listar fotos, zip por loja."""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUDIT_FILE", str(tmp_path / "audit_sessions.json"))
    monkeypatch.setattr(server, "CACHE_FILE", str(tmp_path / "cache_data.json"))
    monkeypatch.setattr(server, "SEED_CACHE_FILE", str(tmp_path / "seed_cache.json"))
    monkeypatch.setattr(server, "USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setattr(server, "DEDUP_FILE", str(tmp_path / "dedup_groups.json"))
    monkeypatch.setattr(server, "FOTOS_FILE", str(tmp_path / "fotos_por_filial.json"))
    monkeypatch.setattr(server, "FOTOS_DIR", str(tmp_path / "fotos"))
    monkeypatch.setattr(server, "_admin_login_attempts", {})
    monkeypatch.setattr(server, "_bipador_login_attempts", {})
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "segredo123")
    server._save_users({})
    server.app.config["TESTING"] = True
    return server.app.test_client()


def _admin():
    return {"adminPassword": "segredo123"}


def test_criar_usuario_com_role_fotografo(client):
    resp = client.post("/api/admin/bipadores", json={
        **_admin(), "name": "Foto1", "email": "foto1@x.com",
        "password": "senha123", "filialId": 1, "role": "fotografo",
    })
    assert resp.status_code == 201
    user = resp.get_json()["user"]
    assert user["role"] == "fotografo"

    saved = server._load_users()
    assert saved["foto1@x.com"]["role"] == "fotografo"


def test_criar_usuario_sem_role_vira_bipador(client):
    resp = client.post("/api/admin/bipadores", json={
        **_admin(), "name": "Bip1", "email": "bip1@x.com",
        "password": "senha123", "filialId": 1,
    })
    assert resp.status_code == 201
    assert resp.get_json()["user"]["role"] == "bipador"


def test_criar_usuario_com_role_invalido_vira_bipador(client):
    resp = client.post("/api/admin/bipadores", json={
        **_admin(), "name": "Bip2", "email": "bip2@x.com",
        "password": "senha123", "filialId": 1, "role": "gerente",
    })
    assert resp.status_code == 201
    assert resp.get_json()["user"]["role"] == "bipador"
```

- [ ] **Step 2: Rodar o teste pra confirmar que falha**

Run: `pytest tests/test_fotografo.py -v`
Expected: `FAIL` — `AttributeError: module 'server' has no attribute 'FOTOS_FILE'` (a fixture já referencia coisas que ainda não existem nas próximas tasks; por enquanto comente as duas linhas de `FOTOS_FILE`/`FOTOS_DIR` na fixture pra rodar só os 3 testes desta task) e/ou `KeyError: 'role'` nos asserts.

- [ ] **Step 3: Implementar o mínimo pra passar**

Em `server.py`, dentro de `admin_create_bipador` (por volta da linha 751-767):

```python
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    filial_id = data.get("filialId")
    password = data.get("password")
    role = data.get("role") if data.get("role") in ("bipador", "fotografo") else "bipador"
    if not name or not email or filial_id is None or not isinstance(password, str) or not password:
        return jsonify({"ok": False, "error": "Nome, email, senha e loja são obrigatórios."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Senha deve ter pelo menos 6 caracteres."}), 400
    with _users_lock:
        users = _load_users()
        if email in users:
            return jsonify({"ok": False, "error": "Email já cadastrado."}), 409
        users[email] = {
            "name": name, "email": email, "filialId": filial_id, "role": role,
            "password_hash": generate_password_hash(password),
        }
        _save_users(users)
        user_public = {k: v for k, v in users[email].items() if k != "password_hash"}
    return jsonify({"ok": True, "user": user_public}), 201
```

- [ ] **Step 4: Rodar o teste pra confirmar que passa**

Run: `pytest tests/test_fotografo.py -v`
Expected: os 3 testes desta task `PASS` (comente as linhas de `FOTOS_FILE`/`FOTOS_DIR` na fixture por enquanto, elas voltam na Task 2).

- [ ] **Step 5: Rodar a suíte inteira pra garantir que nada quebrou**

Run: `pytest tests/ -v`
Expected: todos os testes existentes continuam `PASS` (o campo `role` novo não é lido em nenhum lugar antigo).

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_fotografo.py
git commit -m "feat: campo role (bipador/fotografo) no cadastro de usuario"
```

---

## Task 2: Armazenamento de fotos + endpoint de fila do fotógrafo

**Files:**
- Modify: `server.py` (novas constantes e rota, perto de `AUDIT_FILE`/`audit_sessions` por volta da linha 60-330)
- Test: `tests/test_fotografo.py`

**Interfaces:**
- Consumes: `_load_audit()` (já existe, devolve `dict[str, session]`), `CACHE["produtos"]` (já existe, lista de dicts com `id`/`descricao`/`ean`/`codproduto`).
- Produces: `FOTOS_FILE` (caminho, `str`), `FOTOS_DIR` (caminho, `str`), `_load_fotos() -> dict`, `_save_fotos(dict) -> None`, `_produtos_bipados_ordenados(filial_id: int) -> list[dict]` (merge de todas as sessões da filial, ordem de bipagem, cada item com `id`/`descricao`/`ean`/`codproduto`), rota `GET /api/fotografo/fila?filialId=<int>` retornando `{"ok": True, "fila": [...], "total_bipado": int, "total_fotografado": int}` (fila = produtos bipados sem foto).

- [ ] **Step 1: Escrever o teste que falha**

Adicionar em `tests/test_fotografo.py` (descomentar as 2 linhas de `FOTOS_FILE`/`FOTOS_DIR` na fixture agora):

```python
def _bipar(client, session_id, produto_id, ean="", descricao="P"):
    client.post("/api/audit/scan", json={
        "sessionId": session_id, "productId": produto_id, "ean": ean, "descricao": descricao,
    })


def test_fila_fotografo_traz_bipados_na_ordem_sem_foto(client):
    server.CACHE["produtos"] = [
        {"id": 10, "descricao": "Produto A", "ean": "111", "codproduto": "A1"},
        {"id": 20, "descricao": "Produto B", "ean": "222", "codproduto": "B1"},
    ]
    r = client.post("/api/audit/session/start", json={
        "filialId": 1, "filialNome": "Loja", "userEmail": "a@a.com", "userName": "A",
    })
    sid = r.get_json()["session"]["id"]
    _bipar(client, sid, 10, "111", "Produto A")
    _bipar(client, sid, 20, "222", "Produto B")

    resp = client.get("/api/fotografo/fila?filialId=1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert [p["id"] for p in body["fila"]] == [10, 20]
    assert body["total_bipado"] == 2
    assert body["total_fotografado"] == 0
    server.CACHE["produtos"] = []


def test_fila_fotografo_sem_filial_id_retorna_400(client):
    resp = client.get("/api/fotografo/fila")
    assert resp.status_code == 400


def test_fila_fotografo_loja_sem_bipagem_retorna_vazia(client):
    resp = client.get("/api/fotografo/fila?filialId=999")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["fila"] == []
    assert body["total_bipado"] == 0
```

- [ ] **Step 2: Rodar o teste pra confirmar que falha**

Run: `pytest tests/test_fotografo.py -v`
Expected: `FAIL` com `404 NOT FOUND` (rota ainda não existe) nos 3 testes novos.

- [ ] **Step 3: Implementar o mínimo pra passar**

Em `server.py`, logo depois do bloco de `_save_audit`/`audit_sessions` (por volta da linha 330, antes da rota `/api/audit/session/start`):

```python
FOTOS_FILE = os.path.join(DATA_DIR, "fotos_por_filial.json")
FOTOS_DIR = os.path.join(DATA_DIR, "fotos")


def _load_fotos():
    try:
        with open(FOTOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_fotos(fotos):
    with open(FOTOS_FILE, "w", encoding="utf-8") as f:
        json.dump(fotos, f, ensure_ascii=False)


def _produtos_bipados_ordenados(filial_id):
    """Mesma logica de getMergedSessionForFilial no frontend: junta os
    'encontrados' de todas as sessoes da filial em ordem cronologica, e
    devolve os produtos do catalogo correspondentes na ordem de bipagem."""
    sessions = _load_audit()
    matches = sorted(
        (s for s in sessions.values() if s.get("filialId") == filial_id),
        key=lambda s: (s.get("data") or "", s.get("inicio") or ""),
    )
    encontrados = {}
    for s in matches:
        encontrados.update(s.get("encontrados") or {})
    produtos_map = {p["id"]: p for p in CACHE.get("produtos", [])}
    resultado = []
    for pid_str in encontrados:
        produto = produtos_map.get(int(pid_str))
        if produto:
            resultado.append(produto)
    return resultado


@app.route("/api/fotografo/fila")
def fotografo_fila():
    filial_id_param = request.args.get("filialId")
    if filial_id_param is None:
        return jsonify({"ok": False, "error": "filialId e obrigatorio."}), 400
    try:
        filial_id = int(filial_id_param)
    except ValueError:
        return jsonify({"ok": False, "error": "filialId deve ser um numero."}), 400

    bipados = _produtos_bipados_ordenados(filial_id)
    fotos = _load_fotos().get(str(filial_id), {})
    fila = [p for p in bipados if str(p["id"]) not in fotos]
    return jsonify({
        "ok": True, "fila": fila,
        "total_bipado": len(bipados), "total_fotografado": len(fotos),
    })
```

- [ ] **Step 4: Rodar o teste pra confirmar que passa**

Run: `pytest tests/test_fotografo.py -v`
Expected: todos os testes até aqui `PASS`.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_fotografo.py
git commit -m "feat: endpoint de fila do fotografo (produtos bipados sem foto)"
```

---

## Task 3: Upload de foto (multipart)

**Files:**
- Modify: `server.py`
- Test: `tests/test_fotografo.py`

**Interfaces:**
- Consumes: `FOTOS_FILE`, `FOTOS_DIR`, `_load_fotos`, `_save_fotos` (Task 2).
- Produces: rota `POST /api/fotografo/foto` (multipart/form-data: `filialId`, `produtoId`, `foto` = arquivo) retornando `{"ok": True, "arquivo": "<filialId>/<produtoId>.jpg"}`; grava em `FOTOS_DIR/<filialId>/<produtoId>.jpg` e atualiza `FOTOS_FILE[str(filialId)][str(produtoId)] = {"arquivo": ..., "fotografadoPor": ..., "fotografadoEm": ...}`.

- [ ] **Step 1: Escrever o teste que falha**

```python
def test_upload_foto_grava_arquivo_e_atualiza_registro(client):
    foto_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    resp = client.post("/api/fotografo/foto", data={
        "filialId": "1", "produtoId": "10", "fotografoEmail": "foto1@x.com",
        "foto": (io.BytesIO(foto_bytes), "foto.jpg"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["arquivo"] == "1/10.jpg"

    caminho = os.path.join(server.FOTOS_DIR, "1", "10.jpg")
    assert os.path.exists(caminho)
    with open(caminho, "rb") as f:
        assert f.read() == foto_bytes

    fotos = server._load_fotos()
    assert fotos["1"]["10"]["arquivo"] == "1/10.jpg"
    assert fotos["1"]["10"]["fotografadoPor"] == "foto1@x.com"
    assert "fotografadoEm" in fotos["1"]["10"]


def test_upload_foto_sobrescreve_arquivo_existente(client):
    def _envia(conteudo):
        return client.post("/api/fotografo/foto", data={
            "filialId": "1", "produtoId": "10", "fotografoEmail": "foto1@x.com",
            "foto": (io.BytesIO(conteudo), "foto.jpg"),
        }, content_type="multipart/form-data")

    _envia(b"primeira-versao")
    _envia(b"segunda-versao")

    caminho = os.path.join(server.FOTOS_DIR, "1", "10.jpg")
    with open(caminho, "rb") as f:
        assert f.read() == b"segunda-versao"
    fotos = server._load_fotos()
    assert len(fotos["1"]) == 1


def test_upload_foto_sem_campos_obrigatorios_retorna_400(client):
    resp = client.post("/api/fotografo/foto", data={"filialId": "1"}, content_type="multipart/form-data")
    assert resp.status_code == 400
```

- [ ] **Step 2: Rodar o teste pra confirmar que falha**

Run: `pytest tests/test_fotografo.py -v`
Expected: `FAIL` com `404 NOT FOUND`.

- [ ] **Step 3: Implementar o mínimo pra passar**

Em `server.py`, logo depois de `fotografo_fila`:

```python
@app.route("/api/fotografo/foto", methods=["POST"])
def fotografo_upload_foto():
    filial_id_raw = request.form.get("filialId")
    produto_id_raw = request.form.get("produtoId")
    fotografo_email = (request.form.get("fotografoEmail") or "").strip().lower()
    arquivo = request.files.get("foto")
    if filial_id_raw is None or produto_id_raw is None or not arquivo:
        return jsonify({"ok": False, "error": "filialId, produtoId e foto sao obrigatorios."}), 400
    try:
        filial_id = int(filial_id_raw)
        produto_id = int(produto_id_raw)
    except ValueError:
        return jsonify({"ok": False, "error": "filialId e produtoId devem ser numeros."}), 400

    pasta_filial = os.path.join(FOTOS_DIR, str(filial_id))
    os.makedirs(pasta_filial, exist_ok=True)
    caminho = os.path.join(pasta_filial, f"{produto_id}.jpg")
    arquivo.save(caminho)

    with _fotos_lock:
        fotos = _load_fotos()
        fotos.setdefault(str(filial_id), {})[str(produto_id)] = {
            "arquivo": f"{filial_id}/{produto_id}.jpg",
            "fotografadoPor": fotografo_email,
            "fotografadoEm": datetime.now().isoformat(),
        }
        _save_fotos(fotos)

    return jsonify({"ok": True, "arquivo": f"{filial_id}/{produto_id}.jpg"})
```

E declarar o lock junto de `FOTOS_FILE`/`FOTOS_DIR` (Task 2), mesmo padrão de `_audit_lock`/`_users_lock`:

```python
FOTOS_FILE = os.path.join(DATA_DIR, "fotos_por_filial.json")
FOTOS_DIR = os.path.join(DATA_DIR, "fotos")
_fotos_lock = threading.Lock()
```

- [ ] **Step 4: Rodar o teste pra confirmar que passa**

Run: `pytest tests/test_fotografo.py -v`
Expected: `PASS`.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_fotografo.py
git commit -m "feat: endpoint de upload de foto por produto (sobrescreve se ja existir)"
```

---

## Task 4: Servir foto + listar fotos de uma loja

**Files:**
- Modify: `server.py`
- Test: `tests/test_fotografo.py`

**Interfaces:**
- Consumes: `FOTOS_DIR`, `_load_fotos` (Tasks 2-3).
- Produces: rota `GET /api/fotos/<int:filial_id>/<int:produto_id>.jpg` (serve o arquivo via `send_file`, 404 se não existir); rota `GET /api/fotografo/fotos?filialId=<int>` retornando `{"ok": True, "fotos": [{"produtoId": int, "url": "/api/fotos/<f>/<p>.jpg", "fotografadoEm": str}, ...]}`.

- [ ] **Step 1: Escrever o teste que falha**

```python
def test_servir_foto_retorna_arquivo(client):
    client.post("/api/fotografo/foto", data={
        "filialId": "1", "produtoId": "10", "fotografoEmail": "foto1@x.com",
        "foto": (io.BytesIO(b"conteudo-da-foto"), "foto.jpg"),
    }, content_type="multipart/form-data")

    resp = client.get("/api/fotos/1/10.jpg")
    assert resp.status_code == 200
    assert resp.data == b"conteudo-da-foto"


def test_servir_foto_inexistente_retorna_404(client):
    resp = client.get("/api/fotos/1/999.jpg")
    assert resp.status_code == 404


def test_listar_fotos_da_loja(client):
    client.post("/api/fotografo/foto", data={
        "filialId": "1", "produtoId": "10", "fotografoEmail": "foto1@x.com",
        "foto": (io.BytesIO(b"a"), "a.jpg"),
    }, content_type="multipart/form-data")
    client.post("/api/fotografo/foto", data={
        "filialId": "1", "produtoId": "20", "fotografoEmail": "foto1@x.com",
        "foto": (io.BytesIO(b"b"), "b.jpg"),
    }, content_type="multipart/form-data")

    resp = client.get("/api/fotografo/fotos?filialId=1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["fotos"]) == 2
    ids = {f["produtoId"] for f in body["fotos"]}
    assert ids == {10, 20}
    assert body["fotos"][0]["url"].startswith("/api/fotos/1/")
```

- [ ] **Step 2: Rodar o teste pra confirmar que falha**

Run: `pytest tests/test_fotografo.py -v`
Expected: `FAIL` com `404 NOT FOUND` nas 2 rotas novas (a diferença entre "não existe rota" e "existe rota mas 404 de arquivo" fica clara pelo teste `test_listar_fotos_da_loja`, que falharia com 404 de rota inexistente antes da implementação).

- [ ] **Step 3: Implementar o mínimo pra passar**

Em `server.py`, logo depois de `fotografo_upload_foto`:

```python
@app.route("/api/fotos/<int:filial_id>/<int:produto_id>.jpg")
def servir_foto(filial_id, produto_id):
    caminho = os.path.join(FOTOS_DIR, str(filial_id), f"{produto_id}.jpg")
    if not os.path.isfile(caminho):
        return jsonify({"ok": False, "error": "Foto nao encontrada."}), 404
    return send_file(caminho, mimetype="image/jpeg")


@app.route("/api/fotografo/fotos")
def fotografo_listar_fotos():
    filial_id_param = request.args.get("filialId")
    if filial_id_param is None:
        return jsonify({"ok": False, "error": "filialId e obrigatorio."}), 400
    try:
        filial_id = int(filial_id_param)
    except ValueError:
        return jsonify({"ok": False, "error": "filialId deve ser um numero."}), 400

    fotos_filial = _load_fotos().get(str(filial_id), {})
    resultado = [
        {
            "produtoId": int(pid),
            "url": f"/api/fotos/{filial_id}/{pid}.jpg",
            "fotografadoEm": info.get("fotografadoEm"),
        }
        for pid, info in fotos_filial.items()
    ]
    return jsonify({"ok": True, "fotos": resultado})
```

- [ ] **Step 4: Rodar o teste pra confirmar que passa**

Run: `pytest tests/test_fotografo.py -v`
Expected: `PASS`.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_fotografo.py
git commit -m "feat: servir foto individual e listar fotos de uma loja"
```

---

## Task 5: Download em `.zip` por loja (admin)

**Files:**
- Modify: `server.py`
- Test: `tests/test_fotografo.py`

**Interfaces:**
- Consumes: `FOTOS_DIR`, `_load_fotos`, `_admin_password_ok` (já existe, usado em todo endpoint admin).
- Produces: rota `POST /api/admin/fotos/zip` (JSON: `adminPassword`, `filialId`) retornando o arquivo `.zip` como anexo (`Content-Disposition: attachment`), 403 se senha errada, 404 se a loja não tem foto nenhuma.

- [ ] **Step 1: Escrever o teste que falha**

```python
def test_zip_fotos_da_loja(client):
    client.post("/api/fotografo/foto", data={
        "filialId": "1", "produtoId": "10", "fotografoEmail": "foto1@x.com",
        "foto": (io.BytesIO(b"conteudo-a"), "a.jpg"),
    }, content_type="multipart/form-data")
    client.post("/api/fotografo/foto", data={
        "filialId": "1", "produtoId": "20", "fotografoEmail": "foto1@x.com",
        "foto": (io.BytesIO(b"conteudo-b"), "b.jpg"),
    }, content_type="multipart/form-data")

    resp = client.post("/api/admin/fotos/zip", json={**_admin(), "filialId": 1})
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"

    zip_bytes = io.BytesIO(resp.data)
    with zipfile.ZipFile(zip_bytes) as zf:
        nomes = set(zf.namelist())
        assert nomes == {"10.jpg", "20.jpg"}
        assert zf.read("10.jpg") == b"conteudo-a"


def test_zip_fotos_senha_errada_retorna_403(client):
    resp = client.post("/api/admin/fotos/zip", json={"adminPassword": "errada", "filialId": 1})
    assert resp.status_code == 403


def test_zip_fotos_loja_sem_fotos_retorna_404(client):
    resp = client.post("/api/admin/fotos/zip", json={**_admin(), "filialId": 999})
    assert resp.status_code == 404
```

- [ ] **Step 2: Rodar o teste pra confirmar que falha**

Run: `pytest tests/test_fotografo.py -v`
Expected: `FAIL` com `404 NOT FOUND` (rota ainda não existe).

- [ ] **Step 3: Implementar o mínimo pra passar**

Adicionar `import io` e `import zipfile` no topo de `server.py` (junto dos outros imports stdlib), e a rota logo depois de `fotografo_listar_fotos`:

```python
@app.route("/api/admin/fotos/zip", methods=["POST"])
def admin_zip_fotos():
    data = request.get_json(silent=True) or {}
    if not _admin_password_ok(data.get("adminPassword")):
        return jsonify({"ok": False, "error": "Senha de administrador incorreta."}), 403
    filial_id = data.get("filialId")
    if filial_id is None:
        return jsonify({"ok": False, "error": "filialId e obrigatorio."}), 400

    fotos_filial = _load_fotos().get(str(filial_id), {})
    if not fotos_filial:
        return jsonify({"ok": False, "error": "Nenhuma foto encontrada para esta loja."}), 404

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for produto_id in fotos_filial:
            caminho = os.path.join(FOTOS_DIR, str(filial_id), f"{produto_id}.jpg")
            if os.path.isfile(caminho):
                zf.write(caminho, arcname=f"{produto_id}.jpg")
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                      download_name=f"fotos_filial_{filial_id}.zip")
```

- [ ] **Step 4: Rodar o teste pra confirmar que passa**

Run: `pytest tests/test_fotografo.py -v`
Expected: `PASS`.

- [ ] **Step 5: Rodar a suíte inteira**

Run: `pytest tests/ -v`
Expected: todos os testes `PASS` (incluindo os pré-existentes).

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_fotografo.py
git commit -m "feat: download em zip das fotos de uma loja (admin)"
```

---

## Task 6: Frontend — seletor de papel + desvio pós-login

**Files:**
- Modify: `templates/index.html`

**Interfaces:**
- Consumes: `handleCreateBipador` (existente, linha ~2146), `onAuthSuccess` (existente, linha ~2207), `S.user` (objeto com `role` vindo do backend a partir da Task 1).
- Produces: `<select id="bip-role">` no formulário de cadastro; `startFotografoInternal()` (nova função, chamada no lugar de `startAuditInternal()` quando `S.user.role === 'fotografo'`); `<div id="fotografo-view">` esqueleto vazio (populado na Task 7).

- [ ] **Step 1: Adicionar o seletor de papel no formulário de cadastro**

Em `templates/index.html`, por volta da linha 297-306, trocar o título e adicionar o `<select>`:

```html
<h3 style="margin-bottom:12px">Novo Usuário</h3>
<div style="display:flex;gap:8px;flex-wrap:wrap">
  <input id="bip-name" placeholder="Nome completo" style="flex:1;min-width:150px">
  <input id="bip-email" type="email" placeholder="Email" style="flex:1;min-width:150px">
  <input id="bip-password" type="password" placeholder="Senha (min. 6 caracteres)" autocomplete="new-password" style="flex:1;min-width:150px">
  <select id="bip-filial" style="flex:1;min-width:150px"><option value="">Carregando lojas...</option></select>
  <select id="bip-role" style="flex:1;min-width:150px">
    <option value="bipador">Bipador</option>
    <option value="fotografo">Fotógrafo</option>
  </select>
  <button class="btn-s" id="btn-create-bipador">Cadastrar</button>
</div>
```

- [ ] **Step 2: Enviar o papel escolhido no cadastro**

Em `handleCreateBipador` (linha ~2146-2171), ler o novo campo e resetá-lo no sucesso:

```javascript
async function handleCreateBipador() {
  clearAuthErrors();
  const name = $('#bip-name').value.trim();
  const email = $('#bip-email').value.trim();
  const password = $('#bip-password').value;
  const filialId = Number($('#bip-filial').value);
  const role = $('#bip-role').value;
  if (!name || !email || !password || !filialId) { showAuthError('bip', 'Preencha todos os campos.'); return; }
  if (password.length < 6) { showAuthError('bip', 'Senha deve ter pelo menos 6 caracteres.'); return; }
  try {
    const resp = await fetch('/api/admin/bipadores', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ adminPassword: S.adminPassword, name, email, password, filialId, role }),
    });
    const r = await resp.json();
    if (!r.ok) { showAuthError('bip', r.error); return; }
    toast('Usuário cadastrado!', 'ok');
    $('#bip-name').value = '';
    $('#bip-email').value = '';
    $('#bip-password').value = '';
    $('#bip-filial').value = '';
    $('#bip-role').value = 'bipador';
    loadBipadoresList();
  } catch (e) {
    showAuthError('bip', 'Erro ao cadastrar usuário: sem conexão com o servidor.');
  }
}
```

- [ ] **Step 3: Adicionar a view esqueleto do fotógrafo**

Em `templates/index.html`, logo depois do fechamento de `<div id="audit-view">` (por volta da linha 293-294), adicionar:

```html
<!-- FOTOGRAFO -->
<div id="fotografo-view" style="display:none"></div>
```

- [ ] **Step 4: Desviar o pós-login por papel**

Em `onAuthSuccess` (linha ~2207-2212):

```javascript
function onAuthSuccess(user) {
  const filial = S.filiais.find(f => f.id === user.filialId);
  S.user = { ...user, filialNome: filial ? (filial.fantasia || filial.razaosocial) : 'Loja ' + user.filialId };
  lsSet(LS_KEYS.state, { email: user.email, name: user.name, filialId: user.filialId });
  if (S.user.role === 'fotografo') startFotografoInternal();
  else startAuditInternal();
}
```

E logo abaixo, uma função `startFotografoInternal()` placeholder mínimo (populada de verdade na Task 7 — aqui só garante que a navegação funciona):

```javascript
async function startFotografoInternal() {
  $('#setup-modal').style.display = 'none';
  $('#app').classList.add('active');
  $$('nav button[data-view]').forEach(b => b.style.display = 'none');
  ['dashboard-view', 'audit-view', 'reports-view', 'debug-view', 'bipadores-view', 'dedup-view'].forEach(id => $(`#${id}`).style.display = 'none');
  $('#fotografo-view').style.display = 'block';
  $('#hdr-filial').textContent = S.user.filialNome;
  $('#hdr-auditor').textContent = S.user.name;
}
```

- [ ] **Step 5: Verificação manual**

Rodar o servidor local (`DATA_DIR` apontando pra uma pasta de teste, como já feito em sessões anteriores deste projeto) e, via Playwright ou navegador:
1. Logar como admin, ir em Bipadores, confirmar que o `<select>` "Bipador/Fotógrafo" aparece.
2. Cadastrar um usuário com papel "Fotógrafo".
3. Deslogar, logar com esse usuário — confirmar que a tela mostra só o cabeçalho (loja/nome) com a área de conteúdo vazia (view esqueleto), sem os botões de navegação do bipador/admin.
4. Cadastrar outro usuário sem mexer no seletor (default "Bipador") — confirmar que ele ainda cai no fluxo de bipagem normal.

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "feat: seletor de papel no cadastro e desvio pos-login pro fotografo"
```

---

## Task 7: Frontend — câmera de captura + fila + envio

**Files:**
- Modify: `templates/index.html`

**Interfaces:**
- Consumes: `GET /api/fotografo/fila?filialId=`, `POST /api/fotografo/foto` (Tasks 2-3), `S.user`, `apiGet` (helper existente).
- Produces: `S.fotografoFila` (array em memória), `loadFotografoFila()`, `renderFotografoAtual()`, `startFotoCamera()`, `capturarFoto()`, `repetirFoto()`, `enviarFotoAtual()`, `#foto-camera-modal` (novo, dedicado — não reaproveita `#camera-modal` do scanner).

- [ ] **Step 1: CSS do modal de câmera de foto**

Em `templates/index.html`, junto do bloco de CSS de `#camera-modal` (por volta da linha 83-87):

```css
#foto-camera-modal{position:fixed;inset:0;background:#000;z-index:300;display:none;flex-direction:column}
#foto-camera-modal.active{display:flex}
#foto-camera-modal video,#foto-camera-modal img{flex:1;width:100%;object-fit:cover}
#foto-camera-modal .cam-controls{position:absolute;bottom:30px;left:50%;transform:translateX(-50%);display:flex;gap:12px}
#foto-camera-modal .close-btn{position:absolute;top:16px;right:16px;background:rgba(255,255,255,.2);color:#fff;border-radius:50%;width:44px;height:44px;font-size:1.5rem;display:flex;align-items:center;justify-content:center}
```

- [ ] **Step 2: HTML da view do fotógrafo e do modal de câmera**

Substituir o esqueleto `<div id="fotografo-view">` (Task 6) por:

```html
<div id="fotografo-view" style="display:none">
  <div style="padding:12px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <strong id="foto-progresso-texto">0 de 0 fotografados</strong>
      <button class="btn-o btn-sm" id="btn-ja-fotografados">Já fotografados</button>
    </div>
    <div style="background:var(--surface-2);border-radius:8px;height:10px;overflow:hidden;margin-bottom:16px">
      <div id="foto-progresso-barra" style="background:var(--p500);height:100%;width:0%;transition:width .3s"></div>
    </div>
    <div id="foto-produto-atual" class="card" style="text-align:center;padding:24px"></div>
    <div style="display:flex;gap:8px;margin-top:12px;justify-content:center">
      <button class="btn-p" id="btn-abrir-camera-foto">Tirar Foto</button>
      <button class="btn-o" id="btn-pular-produto">Pular</button>
      <button class="btn-o" id="btn-ler-codigo-avulso">Ler Código de Barras</button>
    </div>
  </div>
</div>

<div id="foto-camera-modal">
  <video id="foto-camera-video" autoplay playsinline></video>
  <canvas id="foto-canvas" style="display:none"></canvas>
  <img id="foto-preview-img" style="display:none">
  <button class="close-btn" id="btn-fechar-foto-camera">×</button>
  <div class="cam-controls" id="foto-cam-controls-video">
    <button class="btn-p" id="btn-capturar-foto">Capturar</button>
  </div>
  <div class="cam-controls" id="foto-cam-controls-preview" style="display:none">
    <button class="btn-o" id="btn-repetir-foto">Repetir</button>
    <button class="btn-p" id="btn-enviar-foto">Enviar</button>
  </div>
</div>
```

- [ ] **Step 3: Estado e carregamento da fila**

Junto da declaração de `S` (objeto de estado global, procurar `const S = {` ou `let S = {`), adicionar a chave:

```javascript
fotografoFila: [],
fotografoAtual: null,
fotoStream: null,
fotoBlobAtual: null,
```

Nova função, perto de `loadSalesCache`/`loadFase2`:

```javascript
async function loadFotografoFila() {
  const r = await apiGet('fotografo/fila', { filialId: S.user.filialId });
  S.fotografoFila = r.fila || [];
  S.totalBipado = r.total_bipado || 0;
  S.totalFotografado = r.total_fotografado || 0;
  S.fotografoAtual = S.fotografoFila[0] || null;
  renderFotografoAtual();
}

function renderFotografoAtual() {
  $('#foto-progresso-texto').textContent = `${S.totalFotografado} de ${S.totalBipado} fotografados`;
  const pct = S.totalBipado > 0 ? Math.round((S.totalFotografado / S.totalBipado) * 100) : 0;
  $('#foto-progresso-barra').style.width = pct + '%';

  const el = $('#foto-produto-atual');
  if (!S.fotografoAtual) {
    el.innerHTML = '<strong>Loja concluída!</strong><p style="color:var(--muted);margin-top:8px">Todos os produtos bipados já foram fotografados.</p>';
    $('#btn-abrir-camera-foto').disabled = true;
    $('#btn-pular-produto').disabled = true;
    return;
  }
  $('#btn-abrir-camera-foto').disabled = false;
  $('#btn-pular-produto').disabled = false;
  const p = S.fotografoAtual;
  el.innerHTML = `<strong style="font-size:1.1rem">${escapeHtml(p.descricao)}</strong>
    <div style="color:var(--muted);font-size:.85rem;margin-top:4px">SKU: ${escapeHtml(p.codproduto || '-')} | EAN: ${escapeHtml(p.ean || '-')}</div>`;
}
```

Atualizar `startFotografoInternal()` (Task 6) pra chamar `await loadFotografoFila();` no final.

- [ ] **Step 4: Câmera — abrir, capturar, repetir, enviar**

```javascript
async function startFotoCamera() {
  try {
    S.fotoStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1280 } },
      audio: false,
    });
    $('#foto-camera-video').srcObject = S.fotoStream;
    $('#foto-camera-video').style.display = 'block';
    $('#foto-preview-img').style.display = 'none';
    $('#foto-cam-controls-video').style.display = 'flex';
    $('#foto-cam-controls-preview').style.display = 'none';
    $('#foto-camera-modal').classList.add('active');
  } catch (e) {
    toast('Erro ao acessar câmera: ' + e.message, 'err');
  }
}

function stopFotoCamera() {
  if (S.fotoStream) {
    S.fotoStream.getTracks().forEach(t => t.stop());
    S.fotoStream = null;
  }
  $('#foto-camera-modal').classList.remove('active');
  S.fotoBlobAtual = null;
}

function capturarFoto() {
  const video = $('#foto-camera-video');
  const canvas = $('#foto-canvas');
  const maxLado = 1600;
  const escala = Math.min(1, maxLado / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = video.videoWidth * escala;
  canvas.height = video.videoHeight * escala;
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => {
    S.fotoBlobAtual = blob;
    $('#foto-preview-img').src = URL.createObjectURL(blob);
    $('#foto-camera-video').style.display = 'none';
    $('#foto-preview-img').style.display = 'block';
    $('#foto-cam-controls-video').style.display = 'none';
    $('#foto-cam-controls-preview').style.display = 'flex';
  }, 'image/jpeg', 0.85);
}

function repetirFoto() {
  S.fotoBlobAtual = null;
  $('#foto-camera-video').style.display = 'block';
  $('#foto-preview-img').style.display = 'none';
  $('#foto-cam-controls-video').style.display = 'flex';
  $('#foto-cam-controls-preview').style.display = 'none';
}

async function enviarFotoAtual() {
  if (!S.fotoBlobAtual || !S.fotografoAtual) return;
  const produto = S.fotografoAtual;
  const form = new FormData();
  form.append('filialId', S.user.filialId);
  form.append('produtoId', produto.id);
  form.append('fotografoEmail', S.user.email);
  form.append('foto', S.fotoBlobAtual, 'foto.jpg');
  try {
    const resp = await fetch('/api/fotografo/foto', { method: 'POST', body: form });
    const r = await resp.json();
    if (!r.ok) { toast('Erro ao enviar foto: ' + r.error, 'err'); return; }
    toast('Foto enviada!', 'ok');
    stopFotoCamera();
    S.fotografoFila = S.fotografoFila.filter(p => p.id !== produto.id);
    S.totalFotografado += 1;
    S.fotografoAtual = S.fotografoFila[0] || null;
    renderFotografoAtual();
  } catch (e) {
    toast('Falha ao enviar foto: sem conexão com o servidor.', 'err');
  }
}

function pularProdutoFotografo() {
  if (!S.fotografoAtual) return;
  const pulado = S.fotografoFila.shift();
  S.fotografoFila.push(pulado);
  S.fotografoAtual = S.fotografoFila[0] || null;
  renderFotografoAtual();
}
```

- [ ] **Step 5: Ligar os event listeners**

No bloco final de `init()` (onde já ficam os outros `addEventListener`), adicionar:

```javascript
$('#btn-abrir-camera-foto').addEventListener('click', startFotoCamera);
$('#btn-fechar-foto-camera').addEventListener('click', stopFotoCamera);
$('#btn-capturar-foto').addEventListener('click', capturarFoto);
$('#btn-repetir-foto').addEventListener('click', repetirFoto);
$('#btn-enviar-foto').addEventListener('click', enviarFotoAtual);
$('#btn-pular-produto').addEventListener('click', pularProdutoFotografo);
```

- [ ] **Step 6: Verificação manual**

Servidor local rodando, logar como fotógrafo com produtos já bipados na loja dele (usar dados de teste já existentes no volume de dev, como em sessões anteriores):
1. Confirmar que o primeiro produto da fila aparece com nome/SKU/EAN.
2. Clicar "Tirar Foto" — confirmar que o preview de câmera abre (permitir permissão do navegador).
3. Clicar "Capturar" — confirmar que troca pra tela de revisão com a foto tirada e botões Repetir/Enviar.
4. Clicar "Repetir" — confirmar que volta pro preview ao vivo.
5. Capturar de novo e clicar "Enviar" — confirmar toast de sucesso, barra de progresso avança, e o próximo produto da fila aparece automaticamente.
6. Clicar "Pular" num produto — confirmar que ele desaparece da posição atual e o próximo aparece (não precisa verificar que ele volta pro fim nessa etapa manual, mas o clique não pode travar a tela).
7. Repetir até a fila esvaziar — confirmar a mensagem "Loja concluída!".

- [ ] **Step 7: Commit**

```bash
git add templates/index.html
git commit -m "feat: tela do fotografo - fila, camera ao vivo, captura e envio"
```

---

## Task 8: Frontend — leitor de código de barras avulso

**Files:**
- Modify: `templates/index.html`

**Interfaces:**
- Consumes: `findByEanOuCodigo` (já existe, criado na feature de bipagem por código interno), `S.fotografoFila`, `initScannerInput`-like pattern (reaproveita a lógica de scanner já usada na bipagem, não o modal — o fotógrafo já tem seu próprio modal de câmera de FOTO, então o scanner de código aqui é um modal separado e mais simples: só decodifica e fecha).
- Produces: `#foto-scanner-modal` (novo, reaproveitando `S.barcodeDetector` já inicializado em `initBarcodeDetector()`), `abrirLeitorAvulso()`, `pularParaProdutoAvulso(barcode)`.

- [ ] **Step 1: HTML e CSS do modal de leitura avulsa**

Reaproveita o mesmo CSS de `#camera-modal` (já cobre `video`, `.cam-controls`, `.close-btn` via seletor de classe, não precisa duplicar CSS — só o HTML muda de id):

```html
<div id="foto-scanner-modal" style="position:fixed;inset:0;background:#000;z-index:300;display:none;flex-direction:column">
  <video id="foto-scanner-video" autoplay playsinline style="flex:1;width:100%;object-fit:cover"></video>
  <button class="close-btn" id="btn-fechar-foto-scanner" style="position:absolute;top:16px;right:16px;background:rgba(255,255,255,.2);color:#fff;border-radius:50%;width:44px;height:44px;font-size:1.5rem;display:flex;align-items:center;justify-content:center">×</button>
</div>
```

- [ ] **Step 2: JS — abrir scanner, detectar, resolver contra a fila**

```javascript
let fotoScannerStream = null;
let fotoScannerAtivo = false;

async function abrirLeitorAvulso() {
  if (!S.barcodeDetector) { toast('Leitor de código de barras não disponível neste navegador.', 'err'); return; }
  try {
    fotoScannerStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1280 } }, audio: false,
    });
    const video = $('#foto-scanner-video');
    video.srcObject = fotoScannerStream;
    $('#foto-scanner-modal').style.display = 'flex';
    fotoScannerAtivo = true;
    fotoScannerLoop(video);
  } catch (e) {
    toast('Erro ao acessar câmera: ' + e.message, 'err');
  }
}

function fecharLeitorAvulso() {
  fotoScannerAtivo = false;
  if (fotoScannerStream) {
    fotoScannerStream.getTracks().forEach(t => t.stop());
    fotoScannerStream = null;
  }
  $('#foto-scanner-video').srcObject = null;
  $('#foto-scanner-modal').style.display = 'none';
}

function fotoScannerLoop(video) {
  (async function loop() {
    if (!fotoScannerAtivo) return;
    try {
      const barcodes = await S.barcodeDetector.detect(video);
      if (barcodes.length > 0) {
        fecharLeitorAvulso();
        pularParaProdutoAvulso(barcodes[0].rawValue);
        return;
      }
    } catch {}
    requestAnimationFrame(() => fotoScannerLoop(video));
  })();
}

function pularParaProdutoAvulso(codigo) {
  const matches = findByEanOuCodigo(codigo);
  if (!matches || matches.length === 0) {
    toast('❌ Código não encontrado no catálogo.', 'err');
    return;
  }
  const produtoId = matches[0].id;
  const idx = S.fotografoFila.findIndex(p => p.id === produtoId);
  if (idx === -1) {
    toast('Este produto ainda não foi bipado nesta loja.', 'warn');
    return;
  }
  const [produto] = S.fotografoFila.splice(idx, 1);
  S.fotografoFila.unshift(produto);
  S.fotografoAtual = produto;
  renderFotografoAtual();
  toast(`Pulou para: ${produto.descricao}`, 'ok');
}
```

- [ ] **Step 3: Ligar os event listeners**

```javascript
$('#btn-ler-codigo-avulso').addEventListener('click', abrirLeitorAvulso);
$('#btn-fechar-foto-scanner').addEventListener('click', fecharLeitorAvulso);
```

- [ ] **Step 4: Verificação manual**

1. Na tela do fotógrafo, clicar "Ler Código de Barras" — confirmar que abre a câmera em tela cheia.
2. Apontar pra um código de um produto já bipado na loja (mas que não é o atual da fila) — confirmar que fecha o scanner, mostra toast "Pulou para: ..." e o produto vira o atual na tela.
3. Apontar pra um código de produto que não foi bipado nessa loja — confirmar toast de aviso, sem travar.
4. Apontar pra um código totalmente desconhecido — confirmar toast de erro "não encontrado".

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: leitor de codigo de barras avulso pro fotografo pular na fila"
```

---

## Task 9: Frontend — aba "Já fotografados" (revisão/substituição)

**Files:**
- Modify: `templates/index.html`

**Interfaces:**
- Consumes: `GET /api/fotografo/fotos?filialId=` (Task 4), `startFotoCamera`/`enviarFotoAtual` (Task 7, generalizados pra aceitar um produto explícito em vez de sempre `S.fotografoFila[0]`).
- Produces: `#foto-revisar-modal` (lista com miniatura), `abrirJaFotografados()`, `reFotografarProduto(produtoId)`.

- [ ] **Step 1: Generalizar `enviarFotoAtual` pra aceitar refazer foto de um produto específico**

Em `S`, adicionar `fotografoEmRevisao: null` (produto sendo re-fotografado fora da fila principal, `null` quando é o fluxo normal).

Editar `enviarFotoAtual` (Task 7) pra usar `S.fotografoEmRevisao || S.fotografoAtual` como produto-alvo, e só avançar a fila normal quando não for revisão:

```javascript
async function enviarFotoAtual() {
  const produto = S.fotografoEmRevisao || S.fotografoAtual;
  if (!S.fotoBlobAtual || !produto) return;
  const form = new FormData();
  form.append('filialId', S.user.filialId);
  form.append('produtoId', produto.id);
  form.append('fotografoEmail', S.user.email);
  form.append('foto', S.fotoBlobAtual, 'foto.jpg');
  try {
    const resp = await fetch('/api/fotografo/foto', { method: 'POST', body: form });
    const r = await resp.json();
    if (!r.ok) { toast('Erro ao enviar foto: ' + r.error, 'err'); return; }
    toast('Foto enviada!', 'ok');
    stopFotoCamera();
    if (S.fotografoEmRevisao) {
      S.fotografoEmRevisao = null;
      toast('Foto atualizada!', 'ok');
    } else {
      S.fotografoFila = S.fotografoFila.filter(p => p.id !== produto.id);
      S.totalFotografado += 1;
      S.fotografoAtual = S.fotografoFila[0] || null;
      renderFotografoAtual();
    }
  } catch (e) {
    toast('Falha ao enviar foto: sem conexão com o servidor.', 'err');
  }
}
```

- [ ] **Step 2: HTML do modal de revisão**

```html
<div id="foto-revisar-modal" style="position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:250;display:none;align-items:flex-start;justify-content:center;padding:16px;overflow-y:auto">
  <div style="background:var(--surface);border-radius:var(--radius);padding:16px;max-width:600px;width:100%">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <strong>Já Fotografados</strong>
      <button class="btn-o btn-sm" id="btn-fechar-revisar">Fechar</button>
    </div>
    <div id="foto-revisar-lista" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px"></div>
  </div>
</div>
```

- [ ] **Step 3: JS de listagem e re-fotografia**

```javascript
async function abrirJaFotografados() {
  const r = await apiGet('fotografo/fotos', { filialId: S.user.filialId });
  const fotos = r.fotos || [];
  $('#foto-revisar-lista').innerHTML = fotos.length
    ? fotos.map(f => `
      <div style="cursor:pointer;text-align:center" onclick="reFotografarProduto(${f.produtoId})">
        <img src="${f.url}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px">
        <div style="font-size:.7rem;color:var(--muted);margin-top:2px">#${f.produtoId}</div>
      </div>
    `).join('')
    : '<div class="empty">Nenhuma foto enviada ainda.</div>';
  $('#foto-revisar-modal').style.display = 'flex';
}

function reFotografarProduto(produtoId) {
  const produto = S.productsMap[produtoId];
  if (!produto) return;
  S.fotografoEmRevisao = produto;
  $('#foto-revisar-modal').style.display = 'none';
  startFotoCamera();
}
```

- [ ] **Step 4: Ligar os event listeners**

```javascript
$('#btn-ja-fotografados').addEventListener('click', abrirJaFotografados);
$('#btn-fechar-revisar').addEventListener('click', () => $('#foto-revisar-modal').style.display = 'none');
```

- [ ] **Step 5: Verificação manual**

1. Depois de enviar pelo menos 2 fotos (Task 7), clicar "Já fotografados" — confirmar que a lista mostra as miniaturas.
2. Clicar numa miniatura — confirmar que abre a câmera direto (sem passar pela fila principal).
3. Tirar e enviar uma foto nova — confirmar toast "Foto atualizada!" (não "Foto enviada!" nem avanço de fila) e que reabrir "Já fotografados" mostra a miniatura trocada.
4. Confirmar que o contador de progresso da tela principal não mudou depois de re-fotografar (já estava contado).

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "feat: aba Ja Fotografados com revisao e substituicao de foto"
```

---

## Task 10: Frontend — visão do admin (progresso + galeria + zip)

**Files:**
- Modify: `templates/index.html`

**Interfaces:**
- Consumes: `GET /api/fotografo/fila?filialId=` (reaproveitado pra pegar `total_bipado`/`total_fotografado` por loja), `GET /api/fotografo/fotos?filialId=`, `POST /api/admin/fotos/zip`, `S.filiais` (já existe), padrão de download de blob já usado em `exportDedupCSV`.
- Produces: novo card dentro de `#bipadores-view` (por loja: progresso + botão "Ver fotos" + botão "Baixar .zip"), `carregarProgressoFotosPorLoja()`, `abrirGaleriaAdmin(filialId)`, `baixarZipFotos(filialId)`.

- [ ] **Step 1: HTML — card de progresso de fotos por loja**

Em `templates/index.html`, dentro de `#bipadores-view`, logo depois de `<div id="bipadores-list"></div>` (linha ~307):

```html
<div class="card" style="margin-top:16px">
  <h3 style="margin-bottom:12px">Fotografia por Loja</h3>
  <div id="fotos-progresso-list"></div>
</div>

<div id="foto-galeria-admin-modal" style="position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:250;display:none;align-items:flex-start;justify-content:center;padding:16px;overflow-y:auto">
  <div style="background:var(--surface);border-radius:var(--radius);padding:16px;max-width:700px;width:100%">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <strong id="foto-galeria-admin-titulo">Fotos</strong>
      <button class="btn-o btn-sm" id="btn-fechar-galeria-admin">Fechar</button>
    </div>
    <div id="foto-galeria-admin-lista" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px"></div>
  </div>
</div>
```

- [ ] **Step 2: JS — carregar progresso por loja**

```javascript
async function carregarProgressoFotosPorLoja() {
  const linhas = await Promise.all(S.filiais.map(async f => {
    const r = await apiGet('fotografo/fila', { filialId: f.id });
    return { filial: f, totalBipado: r.total_bipado || 0, totalFotografado: r.total_fotografado || 0 };
  }));
  $('#fotos-progresso-list').innerHTML = linhas.map(l => {
    const pct = l.totalBipado > 0 ? Math.round((l.totalFotografado / l.totalBipado) * 100) : 0;
    return `<div class="recent-row" style="cursor:default;flex-direction:column;align-items:stretch;gap:6px">
      <div style="display:flex;justify-content:space-between">
        <strong>${escapeHtml(l.filial.fantasia || l.filial.razaosocial)}</strong>
        <span style="color:var(--muted);font-size:.8rem">${l.totalFotografado} de ${l.totalBipado} (${pct}%)</span>
      </div>
      <div style="background:var(--surface-2);border-radius:8px;height:8px;overflow:hidden">
        <div style="background:var(--p500);height:100%;width:${pct}%"></div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn-o btn-sm" onclick="abrirGaleriaAdmin(${l.filial.id})">Ver fotos</button>
        <button class="btn-s btn-sm" onclick="baixarZipFotos(${l.filial.id})">Baixar .zip</button>
      </div>
    </div>`;
  }).join('');
}

async function abrirGaleriaAdmin(filialId) {
  const filial = S.filiais.find(f => f.id === filialId);
  $('#foto-galeria-admin-titulo').textContent = 'Fotos — ' + (filial ? (filial.fantasia || filial.razaosocial) : filialId);
  const r = await apiGet('fotografo/fotos', { filialId });
  const fotos = r.fotos || [];
  $('#foto-galeria-admin-lista').innerHTML = fotos.length
    ? fotos.map(f => `<a href="${f.url}" target="_blank"><img src="${f.url}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px"></a>`).join('')
    : '<div class="empty">Nenhuma foto ainda.</div>';
  $('#foto-galeria-admin-modal').style.display = 'flex';
}

async function baixarZipFotos(filialId) {
  try {
    const resp = await fetch('/api/admin/fotos/zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ adminPassword: S.adminPassword, filialId }),
    });
    if (!resp.ok) {
      const r = await resp.json().catch(() => ({}));
      toast('Erro ao baixar fotos: ' + (r.error || resp.status), 'err');
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `fotos_filial_${filialId}.zip`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast('Falha ao baixar fotos: sem conexão com o servidor.', 'err');
  }
}
```

- [ ] **Step 3: Ligar listener de fechar galeria e disparar carregamento**

```javascript
$('#btn-fechar-galeria-admin').addEventListener('click', () => $('#foto-galeria-admin-modal').style.display = 'none');
```

No handler que já carrega a view Bipadores pro admin (mesmo lugar onde `loadBipadoresList()` já é chamado ao trocar pra `view === 'bipadores'`), adicionar `carregarProgressoFotosPorLoja();` na mesma sequência.

- [ ] **Step 4: Verificação manual**

1. Como admin, ir na aba Bipadores — confirmar o card "Fotografia por Loja" com uma linha por loja, progresso e barra.
2. Clicar "Ver fotos" numa loja com fotos enviadas (das tasks anteriores) — confirmar que a galeria abre com as miniaturas, clicáveis pra abrir em tamanho real.
3. Clicar "Baixar .zip" — confirmar que baixa um arquivo `.zip` válido (abrir e conferir que tem os `.jpg` dentro).
4. Clicar "Baixar .zip" numa loja sem nenhuma foto — confirmar toast de erro, sem travar a tela.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: admin - progresso, galeria e download zip das fotos por loja"
```

---

## Self-Review

**Cobertura da spec:**
- Papéis e login → Task 1 (backend) + Task 6 (frontend).
- Fila de produtos (ordem de bipagem, menos já fotografados) → Task 2.
- Armazenamento (`DATA_DIR/fotos`, `fotos_por_filial.json`, compressão no navegador) → Tasks 2-3 (backend), Task 7 Step 4 (compressão via `canvas.toBlob` com teto de 1600px e qualidade 0.85).
- Endpoints (`fila`, `foto` POST, `fotos` GET, servir arquivo, zip) → Tasks 2-5.
- Fluxo de captura (preview ao vivo, capturar/repetir/enviar, avança sozinho) → Task 7.
- Pular (reordena local) → Task 7 Step 4 (`pularProdutoFotografo`).
- Leitor avulso (só produtos já bipados) → Task 8.
- Aba "Já fotografados" / substituir foto → Task 9.
- Admin: progresso + galeria + zip → Task 10.
- Fora de escopo (não implementado, conforme spec): múltiplas fotos por produto, gamificação além da barra de progresso, histórico de versões de foto, fotografar produto não bipado, fila persistida no servidor — nenhuma task cobre isso, como esperado.

**Placeholders:** nenhum "TBD"/"implementar depois" — todo step de código tem o código completo.

**Consistência de tipos/nomes:** `role` (string), `filialId`/`produtoId` sempre `int` no backend depois da conversão; `S.fotografoFila` (array de objetos de produto do catálogo, mesmo shape de `CACHE["produtos"]`), `S.fotografoAtual`/`S.fotografoEmRevisao` (objeto de produto ou `null`) usados de forma consistente entre Tasks 7, 8 e 9. `enviarFotoAtual` é definida na Task 7 e **modificada** (não redefinida do zero) na Task 9 — a Task 9 deixa claro que é uma edição da função existente, com o código final completo.
