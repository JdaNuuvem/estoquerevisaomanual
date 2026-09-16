"""Testes do papel fotografo: cadastro com role, fila, upload de foto,
servir/listar fotos, zip por loja."""
import io
import json
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


def _admin_h():
    return {"X-Admin-Password": "segredo123"}


JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _cria_usuario(client, email="foto1@x.com", filial_id=1, role="fotografo"):
    """Cria o usuario e devolve os headers de autenticacao dele."""
    client.post("/api/admin/bipadores", json={
        **_admin(), "name": email, "email": email,
        "password": "senha123", "filialId": filial_id, "role": role,
    })
    r = client.post("/api/auth/login", json={"email": email, "password": "senha123"})
    token = r.get_json()["user"]["token"]
    return {"X-Auth-Email": email, "X-Auth-Token": token}


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


def _bipar(client, session_id, produto_id, ean="", descricao="P"):
    client.post("/api/audit/scan", json={
        "sessionId": session_id, "productId": produto_id, "ean": ean, "descricao": descricao,
    })


def test_fila_fotografo_traz_bipados_na_ordem_sem_foto(client):
    server.CACHE["produtos"] = [
        {"id": 10, "descricao": "Produto A", "ean": "111", "codproduto": "A1"},
        {"id": 20, "descricao": "Produto B", "ean": "222", "codproduto": "B1"},
    ]
    headers = _cria_usuario(client, "a@a.com", 1, "bipador")
    r = client.post("/api/audit/session/start", json={
        "filialId": 1, "filialNome": "Loja", "userEmail": "a@a.com", "userName": "A",
    })
    sid = r.get_json()["session"]["id"]
    _bipar(client, sid, 10, "111", "Produto A")
    _bipar(client, sid, 20, "222", "Produto B")

    resp = client.get("/api/fotografo/fila", headers=headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert [p["id"] for p in body["fila"]] == [10, 20]
    assert body["total_bipado"] == 2
    assert body["total_fotografado"] == 0
    server.CACHE["produtos"] = []


def test_fila_fotografo_sem_autenticacao_retorna_401(client):
    resp = client.get("/api/fotografo/fila")
    assert resp.status_code == 401


def test_centro_midia_so_libera_status_cosmeticos(client, monkeypatch):
    from unittest.mock import patch

    server.CACHE["produtos"] = [
        {"id": 10, "descricao": "Base Forca Extrema 7,5ml", "ean": "7896111994344"},
    ]
    monkeypatch.setattr(server, "_produtos_bipados_ordenados", lambda filial: server.CACHE["produtos"])
    status_headers = _cria_usuario(client, "status@x.com", 63)
    outra_headers = _cria_usuario(client, "outra@x.com", 1)

    with patch.object(server.requests, "get") as internet:
        resp = client.get("/api/fotografo/midia/buscar?produtoId=10", headers=status_headers)
        assert resp.status_code == 200
        assert "7896111994344" in resp.get_json()["googleUrl"]
        assert "google.com/search" in resp.get_json()["googleUrl"]
        internet.assert_not_called()

    resp = client.get("/api/fotografo/midia/buscar?produtoId=10", headers=outra_headers)
    assert resp.status_code == 403
    resp = client.post("/api/fotografo/midia/preparar", headers=outra_headers,
                       json={"produtoId": 10, "url": "https://example.com/foto.jpg"})
    assert resp.status_code == 403
    server.CACHE["produtos"] = []


def test_admin_inicia_automacao_midia_em_lote(client, monkeypatch):
    iniciado = []
    class ThreadFake:
        def __init__(self, target, args, daemon):
            iniciado.append((target, args, daemon))
        def start(self):
            pass
    monkeypatch.setattr(server.threading, "Thread", ThreadFake)
    monkeypatch.setattr(server, "_midia_auto_running", False)
    resp = client.post("/api/admin/midia/automacao", headers=_admin_h(), json={"limit": 20})
    assert resp.status_code == 200
    assert iniciado[0][1] == (20,)
    assert iniciado[0][2] is True


def test_admin_automacao_midia_exige_autenticacao(client):
    assert client.get("/api/admin/midia/automacao").status_code == 401
    assert client.post("/api/admin/midia/automacao", json={"limit": 20}).status_code == 401


def test_busca_automatica_prioriza_nome_e_filtra_resultado_irrelevante(monkeypatch):
    chamadas = []

    class Resposta:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    def get_fake(url, params=None, **kwargs):
        chamadas.append((url, params))
        if "open" in url:
            return Resposta({"product": {}})
        if params["q"] == "SHAMPOO PROHALL DETOX 1L":
            return Resposta({"results": [
                {"title": "Shampoo Prohall Detox 1 Litro", "secure_thumbnail": "https://img.test/certa.jpg"},
                {"title": "Ventilador de mesa", "secure_thumbnail": "https://img.test/errada.jpg"},
            ]})
        return Resposta({"results": []})

    monkeypatch.delenv("MERCADOLIVRE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(server.requests, "get", get_fake)
    opcoes = server._buscar_fotos_produto({
        "descricao": "SHAMPOO PROHALL DETOX 1L", "ean": "7898744860553",
    })
    consultas_ml = [p["q"] for url, p in chamadas if "mercadolibre" in url]
    assert consultas_ml[0] == "SHAMPOO PROHALL DETOX 1L"
    assert [o["url"] for o in opcoes] == ["https://img.test/certa.jpg"]
    assert opcoes[0]["confianca"] == "media"


def test_worker_usa_bipados_preserva_candidatos_e_nao_repete(monkeypatch):
    estados = []
    anterior = {
        "tentadosProdutoIds": [10],
        "candidatos": [{"produtoId": 10, "url": "https://img.test/anterior.jpg"}],
    }
    monkeypatch.setattr(server, "_load_fotos", lambda: {"63": {}})
    monkeypatch.setattr(server, "_load_json_file", lambda *args: anterior)
    monkeypatch.setattr(server, "_produtos_bipados_ordenados", lambda filial: [
        {"id": 10, "descricao": "Ja tentado", "ean": "10"},
        {"id": 20, "descricao": "Novo produto", "ean": "20"},
    ])
    monkeypatch.setattr(server, "_buscar_fotos_produto", lambda produto: [
        {"url": "https://img.test/nova.jpg", "fonte": "teste", "confianca": "media", "contexto": "Novo"}
    ])
    monkeypatch.setattr(server, "_midia_auto_save", lambda state: estados.append(json.loads(json.dumps(state))))
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    server._midia_auto_worker(50)
    final = estados[-1]
    assert final["total"] == 1
    assert final["processados"] == 1
    assert final["tentadosProdutoIds"] == [10, 20]
    assert [c["produtoId"] for c in final["candidatos"]] == [10, 20]


def test_fila_fotografo_admin_sem_filial_id_retorna_400(client):
    resp = client.get("/api/fotografo/fila", headers=_admin_h())
    assert resp.status_code == 400


def test_fila_fotografo_loja_sem_bipagem_retorna_vazia(client):
    resp = client.get("/api/fotografo/fila?filialId=999", headers=_admin_h())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["fila"] == []
    assert body["total_bipado"] == 0


def test_upload_foto_grava_arquivo_e_atualiza_registro(client):
    headers = _cria_usuario(client)
    foto_bytes = JPEG
    resp = client.post("/api/fotografo/foto", data={
        "produtoId": "10", "foto": (io.BytesIO(foto_bytes), "foto.jpg"),
    }, content_type="multipart/form-data", headers=headers)
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
    headers = _cria_usuario(client)

    def _envia(conteudo):
        return client.post("/api/fotografo/foto", data={
            "produtoId": "10", "foto": (io.BytesIO(conteudo), "foto.jpg"),
        }, content_type="multipart/form-data", headers=headers)

    _envia(JPEG + b"primeira")
    _envia(JPEG + b"segunda")

    caminho = os.path.join(server.FOTOS_DIR, "1", "10.jpg")
    with open(caminho, "rb") as f:
        assert f.read() == JPEG + b"segunda"
    fotos = server._load_fotos()
    assert len(fotos["1"]) == 1


def test_upload_foto_sem_campos_obrigatorios_retorna_400(client):
    headers = _cria_usuario(client)
    resp = client.post("/api/fotografo/foto", data={"produtoId": "10"},
                       content_type="multipart/form-data", headers=headers)
    assert resp.status_code == 400


def test_upload_foto_sem_autenticacao_retorna_401(client):
    resp = client.post("/api/fotografo/foto", data={
        "produtoId": "10", "foto": (io.BytesIO(JPEG), "foto.jpg"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 401
    assert not os.path.exists(os.path.join(server.FOTOS_DIR, "1", "10.jpg"))


def test_upload_foto_token_invalido_retorna_401(client):
    _cria_usuario(client)
    resp = client.post("/api/fotografo/foto", data={
        "produtoId": "10", "foto": (io.BytesIO(JPEG), "foto.jpg"),
    }, content_type="multipart/form-data",
        headers={"X-Auth-Email": "foto1@x.com", "X-Auth-Token": "token-falso"})
    assert resp.status_code == 401


def test_upload_foto_ignora_filial_e_email_do_formulario(client):
    """filialId/fotografoEmail do corpo nao podem sobrepor o cadastro - senao
    um fotografo da loja 1 grava foto na loja 2 assinando com outro email."""
    headers = _cria_usuario(client, "foto1@x.com", filial_id=1)
    resp = client.post("/api/fotografo/foto", data={
        "filialId": "2", "produtoId": "10", "fotografoEmail": "chefe@x.com",
        "foto": (io.BytesIO(JPEG), "foto.jpg"),
    }, content_type="multipart/form-data", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["arquivo"] == "1/10.jpg"
    fotos = server._load_fotos()
    assert "2" not in fotos
    assert fotos["1"]["10"]["fotografadoPor"] == "foto1@x.com"


def test_upload_rejeita_arquivo_que_nao_e_imagem_de_verdade(client):
    """mimetype vem do cliente e mente: conteudo executavel declarado como
    image/jpeg tem que ser recusado pelos magic bytes."""
    headers = _cria_usuario(client)
    resp = client.post("/api/fotografo/foto", data={
        "produtoId": "10",
        "foto": (io.BytesIO(b"MZ conteudo executavel"), "foto.jpg", "image/jpeg"),
    }, content_type="multipart/form-data", headers=headers)
    assert resp.status_code == 400
    assert not os.path.exists(os.path.join(server.FOTOS_DIR, "1", "10.jpg"))


def test_fotos_de_outra_loja_nao_vazam_pra_usuario_comum(client):
    """Usuario da loja 2 nao ve as fotos da loja 1, mesmo pedindo filialId=1."""
    h1 = _cria_usuario(client, "foto1@x.com", filial_id=1)
    client.post("/api/fotografo/foto", data={
        "produtoId": "10", "foto": (io.BytesIO(JPEG), "foto.jpg"),
    }, content_type="multipart/form-data", headers=h1)

    h2 = _cria_usuario(client, "foto2@x.com", filial_id=2)
    resp = client.get("/api/fotografo/fotos?filialId=1", headers=h2)
    assert resp.status_code == 200
    assert resp.get_json()["fotos"] == []


def test_servir_foto_retorna_arquivo(client):
    headers = _cria_usuario(client)
    client.post("/api/fotografo/foto", data={
        "produtoId": "10", "foto": (io.BytesIO(JPEG), "foto.jpg"),
    }, content_type="multipart/form-data", headers=headers)

    resp = client.get("/api/fotos/1/10.jpg")
    assert resp.status_code == 200
    assert resp.data == JPEG


def test_servir_foto_inexistente_retorna_404(client):
    resp = client.get("/api/fotos/1/999.jpg")
    assert resp.status_code == 404


def test_listar_fotos_da_loja(client):
    headers = _cria_usuario(client)
    client.post("/api/fotografo/foto", data={
        "produtoId": "10", "foto": (io.BytesIO(JPEG + b"a"), "a.jpg"),
    }, content_type="multipart/form-data", headers=headers)
    client.post("/api/fotografo/foto", data={
        "produtoId": "20", "foto": (io.BytesIO(JPEG + b"b"), "b.jpg"),
    }, content_type="multipart/form-data", headers=headers)

    resp = client.get("/api/fotografo/fotos", headers=headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["fotos"]) == 2
    ids = {f["produtoId"] for f in body["fotos"]}
    assert ids == {10, 20}
    assert body["fotos"][0]["url"].startswith("/api/fotos/1/")


def test_listar_fotos_sem_autenticacao_retorna_401(client):
    resp = client.get("/api/fotografo/fotos")
    assert resp.status_code == 401


def test_zip_fotos_da_loja(client):
    headers = _cria_usuario(client)
    client.post("/api/fotografo/foto", data={
        "produtoId": "10", "foto": (io.BytesIO(JPEG + b"conteudo-a"), "a.jpg"),
    }, content_type="multipart/form-data", headers=headers)
    client.post("/api/fotografo/foto", data={
        "produtoId": "20", "foto": (io.BytesIO(JPEG + b"conteudo-b"), "b.jpg"),
    }, content_type="multipart/form-data", headers=headers)

    resp = client.post("/api/admin/fotos/zip", json={**_admin(), "filialId": 1})
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"

    zip_bytes = io.BytesIO(resp.data)
    with zipfile.ZipFile(zip_bytes) as zf:
        nomes = set(zf.namelist())
        assert nomes == {"10.jpg", "20.jpg"}
        assert zf.read("10.jpg") == JPEG + b"conteudo-a"


def test_zip_fotos_senha_errada_retorna_403(client):
    resp = client.post("/api/admin/fotos/zip", json={"adminPassword": "errada", "filialId": 1})
    assert resp.status_code == 403


def test_zip_fotos_loja_sem_fotos_retorna_404(client):
    resp = client.post("/api/admin/fotos/zip", json={**_admin(), "filialId": 999})
    assert resp.status_code == 404
