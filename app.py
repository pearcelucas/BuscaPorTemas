"""
Backend da Prognose Jurídica — proxy real (não simulado) para a API Pública
do DataJud (CNJ), pronto para deploy no Vercel (Python runtime / FastAPI).

Implementa exatamente os 3 endpoints descritos na seção 4 do documento
"Arquitetura DataJud" (v2):
  GET /api/tribunais/{alias}/varas
  GET /api/temas
  GET /api/processos?tribunal=&vara=&tema=&pagina=

Cache: em vez de um Redis separado, usa o cache de borda do próprio Vercel
via header Cache-Control (s-maxage). Isso evita bater no DataJud a cada
requisição repetida, sem precisar de mais nenhuma peça de infraestrutura —
suficiente para o volume de uso de um MVP. Se o uso crescer a ponto de
precisar de invalidação manual ou cache compartilhado entre regiões, trocar
por um Redis do Vercel Marketplace (ex.: Upstash) é a evolução natural.

Deploy:
  1. pip install fastapi requests  (localmente, para testar: uvicorn app:app --reload)
  2. vercel deploy  (a partir desta pasta, com a Vercel CLI instalada e logada)
  3. Defina DATAJUD_APIKEY como variável de ambiente no projeto Vercel se quiser
     trocar a chave publicada pelo CNJ sem reeditar o código.
"""

import json
import os
import time
from typing import List, Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Configuração — mesma lógica de datajud_client.py, adaptada para servir
# requisições HTTP em vez de rodar como script standalone.
# ---------------------------------------------------------------------------
DATAJUD_APIKEY = os.environ.get(
    "DATAJUD_APIKEY",
    "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",
)
BASE_URL = "https://api-publica.datajud.cnj.jus.br"
HEADERS = {"Authorization": f"APIKey {DATAJUD_APIKEY}", "Content-Type": "application/json"}

TRIBUNAL_ALIASES = {
    "TJSP": "tjsp", "TJRJ": "tjrj", "TJMG": "tjmg",
    "TRT2": "trt2", "TJDFT": "tjdft", "TJRS": "tjrs",
}

CONSULTA_PUBLICA = {
    "TJSP": "https://esaj.tjsp.jus.br/cpopg/open.do",
    "TRT2": "https://pje.trt2.jus.br/consultaprocessual/consulta-cidadao",
}

# Só temas com código de assunto confirmado no CSV oficial do CNJ entram aqui.
# Ver seção 6 do documento de arquitetura para os pendentes.
TEMAS_VERIFICADOS = {
    "dano_moral": {"nome": "Indenização por Dano Moral", "assuntos": [14010]},
    "acidente_trabalho": {"nome": "Acidente de Trabalho", "assuntos": [14012, 14016, 14048, 14194, 14810]},
    "despejo": {"nome": "Despejo por Inadimplemento", "assuntos": [14915]},
    "repeticao_indebito": {"nome": "Repetição do Indébito", "assuntos": [14925]},
}

app = FastAPI(title="Prognose Jurídica — backend DataJud")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrinja ao domínio real do frontend antes de ir para produção
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _post_datajud(endpoint: str, body: dict, tentativas: int = 4) -> dict:
    """POST com backoff exponencial — trata 429/503 sem repassar cru ao cliente."""
    url = f"{BASE_URL}/{endpoint}/_search"
    espera = 1.5
    ultimo_erro = None
    for _ in range(tentativas):
        try:
            resp = requests.post(url, headers=HEADERS, data=json.dumps(body), timeout=20)
        except requests.RequestException as e:
            ultimo_erro = e
            time.sleep(espera)
            espera *= 2
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 503):
            time.sleep(espera)
            espera *= 2
            continue
        raise HTTPException(status_code=502, detail=f"DataJud respondeu {resp.status_code}: {resp.text[:300]}")
    raise HTTPException(status_code=504, detail=f"DataJud indisponível após {tentativas} tentativas ({ultimo_erro})")


@app.get("/api/temas")
def listar_temas(response: Response):
    response.headers["Cache-Control"] = "public, s-maxage=3600"
    return {
        "temas": [
            {"id": key, "nome": val["nome"], "status": "verificado"}
            for key, val in TEMAS_VERIFICADOS.items()
        ]
    }


@app.get("/api/tribunais/{alias}/varas")
def listar_varas(alias: str, response: Response, tamanho: int = 200):
    alias = alias.upper()
    if alias not in TRIBUNAL_ALIASES:
        raise HTTPException(status_code=404, detail=f"Tribunal '{alias}' não configurado.")
    endpoint = f"api_publica_{TRIBUNAL_ALIASES[alias]}"
    body = {"size": 0, "aggs": {"varas": {"terms": {"field": "orgaoJulgador.nome.keyword", "size": tamanho}}}}
    data = _post_datajud(endpoint, body)
    buckets = data.get("aggregations", {}).get("varas", {}).get("buckets", [])
    response.headers["Cache-Control"] = "public, s-maxage=86400, stale-while-revalidate=3600"
    return {
        "tribunal": alias,
        "varas": [{"nome": b["key"], "processosIndexados": b["doc_count"]} for b in buckets],
    }


@app.get("/api/processos")
def buscar_processos(
    response: Response,
    tribunal: str = Query(..., description="Alias do tribunal, ex.: TJSP"),
    vara: str = Query(..., description="Nome exato da vara, vindo de /api/tribunais/{alias}/varas"),
    tema: str = Query(..., description="Chave do tema, vinda de /api/temas"),
    pagina: int = Query(1, ge=1),
    tamanho: int = Query(20, ge=1, le=100),
):
    tribunal = tribunal.upper()
    if tribunal not in TRIBUNAL_ALIASES:
        raise HTTPException(status_code=404, detail=f"Tribunal '{tribunal}' não configurado.")
    if tema not in TEMAS_VERIFICADOS:
        raise HTTPException(
            status_code=422,
            detail=f"Tema '{tema}' ainda não tem código de assunto confirmado — veja a seção 6 da arquitetura.",
        )

    endpoint = f"api_publica_{TRIBUNAL_ALIASES[tribunal]}"
    assuntos = TEMAS_VERIFICADOS[tema]["assuntos"]

    query = {
        "size": tamanho,
        "from": (pagina - 1) * tamanho,
        "query": {"bool": {"must": [
            {"match": {"orgaoJulgador.nome": vara}},
            {"terms": {"assuntos.codigo": assuntos}},
        ]}},
        "sort": [{"@timestamp": {"order": "desc"}}],
    }
    data = _post_datajud(endpoint, query)
    hits = data.get("hits", {}).get("hits", [])

    processos = []
    for h in hits:
        src = h["_source"]
        movimentos = src.get("movimentos") or []
        processos.append({
            "numeroProcesso": src.get("numeroProcesso"),
            "classe": (src.get("classe") or {}).get("nome"),
            "assuntos": [a.get("nome") for a in src.get("assuntos", [])],
            "orgaoJulgador": (src.get("orgaoJulgador") or {}).get("nome"),
            "grau": src.get("grau"),
            "dataAjuizamento": src.get("dataAjuizamento"),
            "ultimoMovimento": movimentos[-1].get("nome") if movimentos else None,
        })

    response.headers["Cache-Control"] = "public, s-maxage=21600, stale-while-revalidate=3600"
    return {
        "tribunal": tribunal,
        "vara": vara,
        "tema": TEMAS_VERIFICADOS[tema]["nome"],
        "pagina": pagina,
        "total_nesta_pagina": len(processos),
        "consultaPublica": CONSULTA_PUBLICA.get(
            tribunal,
            f"Busque \"consulta processual {tribunal}\" no site oficial do tribunal — URL ainda não confirmada.",
        ),
        "processos": processos,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}
