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


def _bipar(client, session_id, produto_id, ean="", descricao="P"):
    client.post("/api/audit/scan", json={
        "sessionId": session_id, "productId": produto_id, "ean": ean, "descricao": descricao,
    })


def test_fila_fotografo_traz_bipados_na_ordem_sem_foto(client):
    server.CACHE["produtos"] = [
        {"id": 10, "descricao": "Produto A", "ean": "111", "codproduto": "A1"},
        {"id": 20, "descricao": "Produto B", "ean": "222", "codproduto": "B1"},
    ]
    server._save_users({"a@a.com": {"name": "A", "filialId": 1, "role": "bipador"}})
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


def test_listar_fotos_sem_filial_id_retorna_400(client):
    resp = client.get("/api/fotografo/fotos")
    assert resp.status_code == 400


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
